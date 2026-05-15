from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="")

    env: Literal["local", "shadow", "prod"] = "local"
    log_level: str = "INFO"
    data_dir: Path = Path("/data")

    # Public base URL of this Syncfox install — used to construct OAuth
    # redirect URIs. Set this to the URL the operator's Caddy / nginx /
    # Cloudflare-tunnel fronts. Defaults to localhost so a fresh `docker
    # compose up` is functional out of the box for local-only setups.
    public_base_url: str = "http://localhost:8081"

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_url: str | None = None  # auto-derived from public_base_url if unset

    dropbox_oauth_client_id: str | None = None
    dropbox_oauth_client_secret: str | None = None
    dropbox_oauth_redirect_url: str | None = None  # auto-derived from public_base_url if unset

    age_secret_key: str | None = None  # for token-at-rest encryption

    # Syncfox Phase 1 — days-remaining badge thresholds + nudge cadence.
    # Operator-tunable via .env. Defaults match the implementation plan.
    syncfox_badge_orange_days: int = 5
    syncfox_badge_red_days: int = 2
    syncfox_nudge_days: int = 25
    # Discord webhook for the daily expiry nudge. No-op if unset (operator
    # opted out of Discord notifications).
    syncfox_discord_webhook_url: str | None = None
    # Local TZ + hour for the daily nudge cron.
    syncfox_nudge_local_tz: str = "America/Edmonton"
    syncfox_nudge_local_hour: int = 9


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    # Derive provider OAuth redirect URLs from public_base_url when the
    # operator hasn't overridden them explicitly. Keeps onboarding simple
    # for self-hosted users — they only need to set PUBLIC_BASE_URL.
    base = s.public_base_url.rstrip("/")
    if s.google_oauth_redirect_url is None:
        object.__setattr__(s, "google_oauth_redirect_url",
                            f"{base}/remotes/oauth/callback/google")
    if s.dropbox_oauth_redirect_url is None:
        object.__setattr__(s, "dropbox_oauth_redirect_url",
                            f"{base}/remotes/oauth/callback/dropbox")
    return s
