"""Per-pair background watchers — Drive-watermark + max-idle-floor flavour.

For each unpaused pair, one asyncio task runs a poll loop:

  every poll_seconds:
    if pair.source_remote is a Google remote with a SA key file we know:
        - if no drive_changes_token yet → fetch one, do a defensive bisync
        - else → ask Drive "anything since drive_changes_token?"
            - if changes seen: trigger bisync, save new token
            - if no changes: SKIP bisync (cheap path) UNLESS we've gone
                             max_idle_seconds without a full bisync —
                             then trigger one anyway (catches iCloud-side
                             changes until phase (2) ships)
    else (any non-Google source):
        - trigger bisync (today's behaviour, untouched)

The trigger function is the shared `trigger_run` from routes.pairs, so a
manual "Run now" and a watcher tick contend on the same per-pair flock.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from cloud_sync.persistence import repos
from cloud_sync.providers import google_changes
from cloud_sync.sync import fingerprint

logger = logging.getLogger(__name__)

# Until (2) ships, we still want iCloud-side changes to propagate.
# Force a full bisync if we haven't done one in this many seconds even
# if Drive's watermark says nothing changed.
MAX_IDLE_SECONDS = 300  # 5 min


@dataclass(slots=True)
class _WatchedPair:
    pair_id: str
    poll_seconds: int
    task: asyncio.Task


class WatcherManager:
    def __init__(self, *, app, trigger_fn: Callable[[str, str, bool], Awaitable[None]]):
        """`trigger_fn(pair_id, label, force_bisync)` runs a bisync.
        `app` is the FastAPI app; we use it to reach app.state.{db, settings}.
        """
        self._app = app
        self._trigger = trigger_fn
        self._watched: dict[str, _WatchedPair] = {}

    def is_watched(self, pair_id: str) -> bool:
        return pair_id in self._watched

    def start(self, pair_id: str, poll_seconds: int) -> None:
        if pair_id in self._watched:
            return
        task = asyncio.create_task(self._loop(pair_id, poll_seconds))
        self._watched[pair_id] = _WatchedPair(pair_id, poll_seconds, task)
        logger.info("watcher started for pair=%s every %ds", pair_id[:8], poll_seconds)

    def stop(self, pair_id: str) -> None:
        wp = self._watched.pop(pair_id, None)
        if wp is None:
            return
        wp.task.cancel()
        logger.info("watcher stopped for pair=%s", pair_id[:8])

    def stop_all(self) -> None:
        for pair_id in list(self._watched.keys()):
            self.stop(pair_id)

    async def _loop(self, pair_id: str, poll_seconds: int) -> None:
        try:
            while True:
                await asyncio.sleep(poll_seconds)
                try:
                    await self._tick(pair_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("watcher tick failed for pair=%s: %s",
                                   pair_id[:8], e)
        except asyncio.CancelledError:
            return

    async def _tick(self, pair_id: str) -> None:
        db = self._app.state.db
        settings = self._app.state.settings
        pair = repos.get_pair(db, pair_id)
        if pair is None or pair["paused"] or not pair["initial_resync_done"]:
            return

        # Decide whether we have a cheap "did anything change on the source side?"
        # check for this pair. Right now we only have one for Google.
        source_remote = pair["source_remote"]
        provider = self._provider_for(source_remote)

        if provider == "google":
            sa_key_path = settings.data_dir / "credentials" / f"{source_remote}.json"
            if not sa_key_path.exists():
                logger.warning("watcher: SA key missing for %s, falling back to blind bisync",
                               source_remote)
                await self._trigger(pair_id, "poll-detected", True)
                repos.mark_full_bisync_done(db, pair_id)
                return

            await self._tick_google(pair, sa_key_path)
        else:
            # Non-Google source — no cheap change-API. Blind bisync.
            await self._trigger(pair_id, "poll-detected", True)
            repos.mark_full_bisync_done(self._app.state.db, pair_id)

    async def _tick_google(self, pair, sa_key_path: Path) -> None:
        db = self._app.state.db
        settings = self._app.state.settings
        pair_id = pair["id"]
        token = pair["drive_changes_token"]

        # First time seeing this pair after (1) lands — seed the watermark
        # and do a defensive bisync so we don't miss anything.
        if not token:
            try:
                fresh = await google_changes.start_page_token(sa_key_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("watcher: start_page_token failed for %s: %s — falling back",
                               pair_id[:8], e)
                await self._trigger(pair_id, "poll-detected", True)
                repos.mark_full_bisync_done(db, pair_id)
                return
            repos.set_drive_watermark(db, pair_id, fresh)
            logger.info("watcher: seeded drive watermark for %s = %s",
                        pair_id[:8], fresh)
            await self._trigger(pair_id, "poll-detected", True)
            repos.mark_full_bisync_done(db, pair_id)
            return

        # Cheap path
        try:
            new_token, seen = await google_changes.changed_since(sa_key_path, token)
        except google_changes.TokenExpired:
            # Token rotted; refetch and force a defensive bisync.
            fresh = await google_changes.start_page_token(sa_key_path)
            repos.set_drive_watermark(db, pair_id, fresh)
            logger.info("watcher: drive token expired for %s, refetched; forcing bisync",
                        pair_id[:8])
            await self._trigger(pair_id, "poll-detected", True)
            repos.mark_full_bisync_done(db, pair_id)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("watcher: changed_since failed for %s: %s — fallback bisync",
                           pair_id[:8], e)
            await self._trigger(pair_id, "poll-detected", True)
            repos.mark_full_bisync_done(db, pair_id)
            return

        if seen > 0:
            # Drive reports activity — trigger bisync, advance watermark
            logger.info("watcher: %d drive changes for pair=%s — bisyncing",
                        seen, pair_id[:8])
            await self._trigger(pair_id, "poll-detected", False)
            repos.set_drive_watermark(db, pair_id, new_token)
            repos.mark_full_bisync_done(db, pair_id)
            return

        # Drive said nothing changed. Now check the destination side
        # (iCloud) via cheap fingerprint — covers files edited directly on
        # phone/Mac while Drive sat idle.
        dest_remote = pair["destination_remote"]
        dest_path = pair["destination_path"]
        dest_provider = self._provider_for(dest_remote)

        if dest_provider == "icloud":
            fp_path = f"{dest_remote}:{dest_path}"
            new_fp = await fingerprint.compute(fp_path, conf_path=settings.data_dir / "rclone.conf")
            if new_fp is None:
                # Couldn't fingerprint (network blip / iCloud trust expired).
                # Fall back to idle-floor logic so we still sync eventually.
                pass
            elif new_fp != (pair["icloud_fingerprint"] or ""):
                logger.info("watcher: icloud fingerprint changed for %s — bisyncing",
                            pair_id[:8])
                await self._trigger(pair_id, "poll-detected", False)
                repos.set_icloud_fingerprint(db, pair_id, new_fp)
                repos.mark_full_bisync_done(db, pair_id)
                return

        # Either no iCloud destination, or fingerprint unchanged.
        # Idle-floor check kept as a safety net (fingerprint can lie if
        # rclone reported a stale listing; full bisync at least once per
        # MAX_IDLE_SECONDS guarantees eventual convergence).
        last_full = pair["last_full_bisync_at"]
        if last_full:
            last_full_dt = datetime.fromisoformat(last_full).replace(tzinfo=timezone.utc)
            idle = (datetime.now(timezone.utc) - last_full_dt).total_seconds()
        else:
            idle = float("inf")
        idle_str = "never" if idle == float("inf") else f"{int(idle)}s"

        if idle >= MAX_IDLE_SECONDS:
            logger.info("watcher: floor reached for %s (%s since last full bisync) — forcing one",
                        pair_id[:8], idle_str)
            await self._trigger(pair_id, "poll-detected", False)
            repos.mark_full_bisync_done(db, pair_id)
            return

        # Both Drive watermark unchanged AND iCloud fingerprint unchanged.
        logger.info("watcher: pair=%s idle SKIP (drive watermark + icloud fingerprint both unchanged, last full %s ago)",
                    pair_id[:8], idle_str)

    def _provider_for(self, remote_name: str) -> str:
        """Look up the provider for a remote name from the DB."""
        row = repos.get_remote(self._app.state.db, remote_name)
        return row["provider"] if row else ""
