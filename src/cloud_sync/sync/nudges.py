"""Daily token-expiry nudge — posts to a Discord webhook for any iCloud
remote whose trust token expires within `SYNCFOX_NUDGE_DAYS` days.

Phase 1 (Syncfox). Manual click is the only way to actually re-auth (per
Sahir's "no auto-trigger" decision); this module just nags.

Implementation notes:
- Single asyncio task seeded from the FastAPI lifespan. Sleeps until the
  next configured local-hour wallclock, fires, sleeps again. No
  APScheduler dep just for one job.
- If `SYNCFOX_DISCORD_WEBHOOK_URL` is unset, the task no-ops (just logs).
- Uses local TZ from `SYNCFOX_NUDGE_LOCAL_TZ` (default America/Edmonton)
  so the post lands at a sensible hour for Sahir; configurable for
  public users elsewhere.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from cloud_sync.config import Settings
from cloud_sync.persistence import repos
from cloud_sync.providers import expiry as expiry_provider

logger = logging.getLogger(__name__)


async def daily_nudge_loop(db: sqlite3.Connection, settings: Settings) -> None:
    """Run forever — fire once per day at the configured local hour."""
    tz = ZoneInfo(settings.syncfox_nudge_local_tz)
    while True:
        # Compute seconds until the next firing.
        now = datetime.now(tz)
        target = now.replace(hour=settings.syncfox_nudge_local_hour,
                             minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        logger.info("nudge_loop: sleeping %.0fs until %s", sleep_s, target.isoformat())
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            logger.info("nudge_loop: cancelled")
            raise
        try:
            await fire_once(db, settings)
        except Exception as e:  # noqa: BLE001
            logger.exception("nudge_loop fire failed: %s", e)


async def fire_once(db: sqlite3.Connection, settings: Settings) -> int:
    """Check every iCloud remote; post a nudge for each one ≤ nudge threshold.
    Returns the count of nudges posted."""
    rows = repos.list_remotes(db)
    expiring: list[tuple[str, int, str]] = []  # (name, days_left, account)
    for r in rows:
        if r["provider"] != "icloud":
            continue
        days = expiry_provider.days_remaining(r["provider"], r["last_verified_at"])
        if days is None:
            continue
        if days <= settings.syncfox_nudge_days:
            expiring.append((r["name"], days, r["account_label"] or "(unknown account)"))
    if not expiring:
        logger.info("nudge_loop: nothing to nudge — %d iCloud remotes all healthy",
                    sum(1 for r in rows if r["provider"] == "icloud"))
        return 0

    if not settings.syncfox_discord_webhook_url:
        logger.info(
            "nudge_loop: %d remote(s) need attention but SYNCFOX_DISCORD_WEBHOOK_URL "
            "is unset — UI badge is your only nudge",
            len(expiring),
        )
        return 0

    lines = ["🦊 **Syncfox — iCloud token expiry nudge**", ""]
    for name, days, account in expiring:
        verb = "expired" if days < 0 else ("expires today" if days == 0 else f"expires in {days}d")
        lines.append(f"- `{name}` ({account}) — {verb}. Hit **Re-authenticate** in Syncfox.")
    body = "\n".join(lines)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                settings.syncfox_discord_webhook_url,
                json={"content": body[:1900]},
            )
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.warning("nudge_loop: webhook post failed: %s", e)
        return 0

    logger.info("nudge_loop: posted %d expiry nudge(s)", len(expiring))
    return len(expiring)
