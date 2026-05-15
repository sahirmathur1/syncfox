from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cloud_sync.config import get_settings
from cloud_sync.persistence.db import open_db
from cloud_sync.routes import dropbox_setup, health, icloud_setup, index, pairs, remotes, setup
from cloud_sync.routes.pairs import attach_watchers, restart_watchers
from cloud_sync.sync import conf_writer, nudges, vaultwarden
import asyncio


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger("cloud_sync.main")

    # Phase 2 (Syncfox) — optional Vaultwarden secrets backend. No-op
    # unless VAULTWARDEN_URL/ITEM_ID/EMAIL/MASTER_PASSWORD all set.
    vaultwarden.maybe_fetch(settings)

    db_path = settings.data_dir / "cloudsync.db"
    db = open_db(db_path)
    log.info("opened cloud-sync db at %s", db_path)

    app.state.settings = settings
    app.state.db = db
    app.state.watchers = attach_watchers(app)

    # Rebuild rclone.conf from rclone_remotes (idempotent; covers cold start
    # after a restart where the conf might have rotted out of sync with the DB).
    if settings.age_secret_key:
        try:
            await conf_writer.rebuild(
                db,
                conf_path=settings.data_dir / "rclone.conf",
                age_secret_key=settings.age_secret_key,
                credentials_dir=settings.data_dir / "credentials",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not rebuild rclone.conf at startup: %s", e)
    else:
        log.warning("AGE_SECRET_KEY not set — encrypted remote configs will not decrypt")

    # Seed watchers from DB now that everything's wired
    restart_watchers(app)

    # Phase 1 (Syncfox) — daily expiry-nudge cron. Single asyncio task,
    # sleeps to next configured wallclock hour, fires, repeats. No-ops if
    # SYNCFOX_DISCORD_WEBHOOK_URL is unset.
    nudge_task = asyncio.create_task(nudges.daily_nudge_loop(db, settings))
    log.info(
        "nudge cron seeded — fires daily at %02d:00 %s, threshold ≤%dd, webhook=%s",
        settings.syncfox_nudge_local_hour,
        settings.syncfox_nudge_local_tz,
        settings.syncfox_nudge_days,
        "set" if settings.syncfox_discord_webhook_url else "unset",
    )

    try:
        yield
    finally:
        nudge_task.cancel()
        try:
            await nudge_task
        except (asyncio.CancelledError, Exception):
            pass
        app.state.watchers.stop_all()
        db.close()


app = FastAPI(title="Syncfox", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(index.router)
app.include_router(setup.router)  # /setup wizard
app.include_router(icloud_setup.router)  # must come before remotes.router so /remotes/connect/icloud isn't shadowed
app.include_router(dropbox_setup.router)  # /remotes/connect/dropbox + OAuth callback
app.include_router(remotes.router)
app.include_router(pairs.router)
