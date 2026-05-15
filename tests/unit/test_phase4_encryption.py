"""Phase 4 (Syncfox) — opt-in Fernet encryption tests."""
from __future__ import annotations

import os

import pytest

from cloud_sync.sync import encryption


@pytest.fixture(autouse=True)
def _clear_env_and_cache(monkeypatch):
    """Each test starts with no master password set + a cleared Fernet cache."""
    monkeypatch.delenv("SYNCFOX_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("AGE_SECRET_KEY", raising=False)
    encryption.reset_cache()
    yield
    encryption.reset_cache()


@pytest.mark.asyncio
async def test_passthrough_when_no_password() -> None:
    """No env var set → encrypt + decrypt are no-ops (legacy mode)."""
    plain = '{"apple_id": "x@y.com", "trust_token": "abc"}'
    enc = await encryption.encrypt(plain)
    dec = await encryption.decrypt(enc)
    assert enc == plain  # no-op
    assert dec == plain


@pytest.mark.asyncio
async def test_round_trip_with_password(monkeypatch) -> None:
    monkeypatch.setenv("SYNCFOX_MASTER_PASSWORD", "correct-horse-battery-staple")
    encryption.reset_cache()

    plain = '{"apple_id": "x@y.com", "trust_token": "abc"}'
    enc = await encryption.encrypt(plain)
    assert enc.startswith("enc1:")
    assert plain not in enc  # actually encrypted, not just prefixed
    dec = await encryption.decrypt(enc)
    assert dec == plain


@pytest.mark.asyncio
async def test_decrypt_plaintext_passes_through_for_back_compat(monkeypatch) -> None:
    """Existing plaintext data (pre-Phase-4) decrypts as a no-op even when
    the password is set — covers the upgrade path where some rows are
    encrypted and others aren't."""
    monkeypatch.setenv("SYNCFOX_MASTER_PASSWORD", "any-password")
    encryption.reset_cache()
    plain = '{"apple_id": "x@y.com"}'
    assert await encryption.decrypt(plain) == plain  # no enc1: prefix → as-is


@pytest.mark.asyncio
async def test_wrong_password_raises(monkeypatch) -> None:
    """Encrypt with one password; switch env to another; decrypt should raise."""
    monkeypatch.setenv("SYNCFOX_MASTER_PASSWORD", "first-password")
    encryption.reset_cache()
    enc = await encryption.encrypt('{"x":1}')

    monkeypatch.setenv("SYNCFOX_MASTER_PASSWORD", "different-password")
    encryption.reset_cache()
    with pytest.raises(RuntimeError, match="does not match"):
        await encryption.decrypt(enc)


@pytest.mark.asyncio
async def test_legacy_age_secret_key_env_still_works(monkeypatch) -> None:
    """Pre-Syncfox installs were already passing AGE_SECRET_KEY — keep it
    working as a fallback so config.py doesn't have to migrate env names."""
    monkeypatch.setenv("AGE_SECRET_KEY", "from-the-old-times")
    encryption.reset_cache()
    enc = await encryption.encrypt('{"x":1}')
    assert enc.startswith("enc1:")
    assert await encryption.decrypt(enc) == '{"x":1}'


@pytest.mark.asyncio
async def test_decrypt_without_password_when_blob_is_encrypted_raises(monkeypatch) -> None:
    """Encrypt with a password, then unset env, then decrypt → RuntimeError."""
    monkeypatch.setenv("SYNCFOX_MASTER_PASSWORD", "one-password")
    encryption.reset_cache()
    enc = await encryption.encrypt('{"x":1}')
    monkeypatch.delenv("SYNCFOX_MASTER_PASSWORD")
    encryption.reset_cache()
    with pytest.raises(RuntimeError, match="no master password"):
        await encryption.decrypt(enc)
