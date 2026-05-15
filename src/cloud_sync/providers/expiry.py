"""Token-expiry helpers — surface days-remaining + badge color per remote.

iCloud trust tokens last ~30 days from the moment Apple issues them. We
treat `rclone_remotes.last_verified_at` as the issued-at marker (it's
updated on every successful auth — both first-time setup and re-auth).
For other providers (Google SA keys, Dropbox refresh tokens) the concept
doesn't apply or works differently; helper returns None and the UI
renders an em-dash.

Phase 1 (Syncfox) introduces this module. Future providers add their own
issued-at semantics by extending `days_remaining` with a per-provider
branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


# Apple's published trust-token lifetime. Empirically Apple often expires
# them slightly earlier (~28 days). 30 is the documented cap.
ICLOUD_TRUST_TOKEN_LIFETIME_DAYS = 30


@dataclass(frozen=True, slots=True)
class ExpiryStatus:
    """What the UI needs to render the badge for one remote."""
    days_remaining: int | None         # None when N/A
    color: Literal["green", "orange", "red", "neutral"]
    label: str                          # e.g. "26d", "expires in 2d", "n/a"


def _parse_sqlite_iso(s: str) -> datetime:
    """SQLite's `datetime('now')` writes 'YYYY-MM-DD HH:MM:SS' (UTC, no tz).
    Parse defensively into an aware datetime."""
    s = s.strip().replace("T", " ")
    # SQLite default has no tz suffix; some callers might add it
    fmt_try = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]
    for fmt in fmt_try:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Fall back to fromisoformat for ISO-like strings
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) \
        if datetime.fromisoformat(s).tzinfo is None else datetime.fromisoformat(s)


def days_remaining(provider: str, last_verified_at: str | None) -> int | None:
    """Return whole days until the credential is expected to expire, or
    None if we don't track expiry for this provider."""
    if provider != "icloud" or not last_verified_at:
        return None
    try:
        issued = _parse_sqlite_iso(last_verified_at)
    except Exception:  # noqa: BLE001 — bad data shouldn't crash the page
        return None
    now = datetime.now(timezone.utc)
    expires_at = issued + _timedelta(days=ICLOUD_TRUST_TOKEN_LIFETIME_DAYS)
    delta = expires_at - now
    # Round DOWN — "1 day left" should mean "more than 24h left", not "0-23h"
    return delta.days


def _timedelta(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def status_for(
    provider: str,
    last_verified_at: str | None,
    *,
    orange_days: int = 5,
    red_days: int = 2,
) -> ExpiryStatus:
    """Bundle days_remaining + a color hint for the UI."""
    days = days_remaining(provider, last_verified_at)
    if days is None:
        return ExpiryStatus(None, "neutral", "n/a")
    if days <= red_days:
        if days < 0:
            return ExpiryStatus(days, "red", f"expired {-days}d ago")
        if days == 0:
            return ExpiryStatus(days, "red", "expires today")
        return ExpiryStatus(days, "red", f"expires in {days}d")
    if days <= orange_days:
        return ExpiryStatus(days, "orange", f"expires in {days}d")
    return ExpiryStatus(days, "green", f"{days}d left")
