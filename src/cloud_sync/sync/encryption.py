"""Token / SA-key persistence — opt-in Fernet encryption.

Phase 4 (Syncfox) replaces the v1 pass-through with real encryption when
the operator sets `SYNCFOX_MASTER_PASSWORD` (or the legacy `AGE_SECRET_KEY`
env var that pre-Syncfox installs were already passing).

Threat model:
- "Someone with read access to /data/cloudsync.db" — encrypted blobs
  read off disk are useless without the master password held in process
  memory. Same protection SQLCipher would give us, without the per-arch
  binary dependency that would complicate Phase 6's multi-arch image.
- "Someone with shell access to a running container" — they can introspect
  process memory or hit /api/* with whatever auth's in front. Encryption-
  at-rest doesn't help here; reverse-proxy auth + OS perms do.

Format: blobs are stored as `enc1:<base64-fernet-token>`. The `enc1:`
prefix lets us upgrade the scheme later (decrypt() falls back to
plaintext for blobs without the prefix — covers existing plaintext
data + opt-out installs).

Backwards-compat: if no master password is set, encrypt() returns
plaintext (as today) and decrypt() also passes through. No change for
installs that haven't opted in.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_PREFIX = "enc1:"


def _master_password() -> str | None:
    pw = os.environ.get("SYNCFOX_MASTER_PASSWORD", "").strip()
    if pw:
        return pw
    legacy = os.environ.get("AGE_SECRET_KEY", "").strip()
    return legacy or None


@lru_cache(maxsize=1)
def _fernet_or_none() -> Fernet | None:
    pw = _master_password()
    if pw is None:
        return None
    # PBKDF2-SHA256 with a fixed app salt — Fernet keys must be 32-byte
    # urlsafe-base64. Static salt (rather than random) means the same
    # master password always derives the same key, so the operator
    # doesn't need to back up a salt file separately.
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pw.encode("utf-8"),
        b"syncfox/encryption/v1",
        200_000,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(digest))


async def encrypt(plaintext: str, secret_key: str | None = None) -> str:
    """Encrypt a credential blob if a master password is configured;
    otherwise return as-is. `secret_key` arg accepted for v1 API back-
    compat; ignored (env var is the source of truth)."""
    f = _fernet_or_none()
    if f is None:
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


async def decrypt(blob: str, secret_key: str | None = None) -> str:
    """Decrypt a credential blob. Plaintext blobs (no prefix) pass
    through so existing data + opt-out installs keep working."""
    if not blob.startswith(_PREFIX):
        return blob
    f = _fernet_or_none()
    if f is None:
        raise RuntimeError(
            "stored blob is encrypted (enc1: prefix) but no master password "
            "is set — set SYNCFOX_MASTER_PASSWORD to decrypt"
        )
    try:
        plaintext_bytes = f.decrypt(blob[len(_PREFIX):].encode("ascii"))
    except InvalidToken as e:
        raise RuntimeError(
            "SYNCFOX_MASTER_PASSWORD does not match the password used to "
            "encrypt this blob — restore the original password OR re-add "
            "the affected provider"
        ) from e
    return plaintext_bytes.decode("utf-8")


def reset_cache() -> None:
    """Test-only: clear the cached Fernet instance so a different env
    can be picked up between tests."""
    _fernet_or_none.cache_clear()
