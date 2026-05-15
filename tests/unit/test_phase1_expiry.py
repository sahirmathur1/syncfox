"""Phase 1 (Syncfox) — token-expiry helper tests.

Covers `cloud_sync.providers.expiry.days_remaining` + `status_for`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cloud_sync.providers import expiry as expiry_provider


def _iso_n_days_ago(n: float) -> str:
    """SQLite-style 'YYYY-MM-DD HH:MM:SS' UTC string n days in the past."""
    t = datetime.now(timezone.utc) - timedelta(days=n)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def test_days_remaining_iCloud_fresh_token_returns_close_to_30() -> None:
    days = expiry_provider.days_remaining("icloud", _iso_n_days_ago(0))
    # Allow ±1d for clock drift / day-boundary rounding
    assert days is not None
    assert 28 <= days <= 30


def test_days_remaining_iCloud_25_day_old_token_returns_about_5() -> None:
    days = expiry_provider.days_remaining("icloud", _iso_n_days_ago(25))
    assert days is not None
    assert 4 <= days <= 5


def test_days_remaining_iCloud_expired_token_returns_negative() -> None:
    days = expiry_provider.days_remaining("icloud", _iso_n_days_ago(35))
    assert days is not None
    assert days < 0


def test_days_remaining_non_icloud_returns_none() -> None:
    assert expiry_provider.days_remaining("google", _iso_n_days_ago(10)) is None
    assert expiry_provider.days_remaining("dropbox", _iso_n_days_ago(10)) is None


def test_days_remaining_no_last_verified_returns_none() -> None:
    assert expiry_provider.days_remaining("icloud", None) is None
    assert expiry_provider.days_remaining("icloud", "") is None


def test_status_for_thresholds() -> None:
    # Fresh token → green
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(0),
                                    orange_days=5, red_days=2)
    assert s.color == "green"
    assert s.days_remaining is not None and s.days_remaining > 5

    # 26 days old (4 left) → orange
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(26),
                                    orange_days=5, red_days=2)
    assert s.color == "orange"
    assert "expires in" in s.label

    # 29 days old (1 left) → red
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(29),
                                    orange_days=5, red_days=2)
    assert s.color == "red"

    # Expired (-3 days) → red, "expired Nd ago"
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(33),
                                    orange_days=5, red_days=2)
    assert s.color == "red"
    assert "expired" in s.label

    # Non-iCloud → neutral
    s = expiry_provider.status_for("google", _iso_n_days_ago(10),
                                    orange_days=5, red_days=2)
    assert s.color == "neutral"
    assert s.label == "n/a"


def test_status_for_custom_thresholds() -> None:
    """Operator-tunable thresholds via env vars (Phase 1 DoD)."""
    # With orange=10, red=2: a 21-day-old token (~9d left) should be ORANGE.
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(21),
                                    orange_days=10, red_days=2)
    assert s.color == "orange", f"got {s.color} ({s.label})"

    # Same token under orange=3, red=1 → green
    s = expiry_provider.status_for("icloud", _iso_n_days_ago(21),
                                    orange_days=3, red_days=1)
    assert s.color == "green", f"got {s.color} ({s.label})"
