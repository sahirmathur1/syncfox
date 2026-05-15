"""Dropbox provider — PKCE OAuth flow + refresh-token storage.

Phase 5 (Syncfox). Dropbox refresh tokens don't expire by default
(unlike iCloud trust tokens), but they CAN be revoked from Dropbox's
side. The expiry-badge surface marks Dropbox remotes as "active" until
something writes a "revoked_at" timestamp into the credential blob.

PKCE flow:
  1. UI button → /remotes/connect/dropbox
  2. Backend generates a code_verifier (random URL-safe), derives
     code_challenge = base64(sha256(code_verifier))
  3. Stash (verifier, target_remote_name) in an in-memory session keyed
     by `state` token; redirect to Dropbox's authorize URL with the
     challenge + state in query string
  4. User approves on dropbox.com; Dropbox redirects to our /callback
     with `code` + `state`
  5. Backend exchanges code+verifier for access+refresh tokens
  6. Persist (encrypted) into rclone_remotes; rebuild rclone.conf

Operator setup: create a Dropbox app at dropbox.com/developers/apps
(scoped or full Dropbox), add `<PUBLIC_BASE_URL>/remotes/oauth/callback/
dropbox` as a redirect URI, set DROPBOX_OAUTH_CLIENT_ID +
DROPBOX_OAUTH_CLIENT_SECRET in .env.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class DropboxCredentials:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    expiry_iso: str            # rclone wants ISO-8601 with trailing Z
    account_email: str          # for the UI's "Account" column
    revoked_at: str = ""        # set if Dropbox returned 401 — UI shows red


def serialize(c: DropboxCredentials) -> str:
    return json.dumps({
        "client_id": c.client_id,
        "client_secret": c.client_secret,
        "access_token": c.access_token,
        "refresh_token": c.refresh_token,
        "expiry_iso": c.expiry_iso,
        "account_email": c.account_email,
        "revoked_at": c.revoked_at,
    })


def deserialize(blob: str) -> DropboxCredentials:
    d = json.loads(blob)
    return DropboxCredentials(
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        access_token=d["access_token"],
        refresh_token=d["refresh_token"],
        expiry_iso=d.get("expiry_iso", "0001-01-01T00:00:00Z"),
        account_email=d.get("account_email", ""),
        revoked_at=d.get("revoked_at", ""),
    )


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64).rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(*, client_id: str, redirect_uri: str, code_challenge: str,
                  state: str) -> str:
    """Build the Dropbox authorize URL the user gets redirected to."""
    from urllib.parse import urlencode
    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "token_access_type": "offline",  # ask Dropbox for a refresh token
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"https://www.dropbox.com/oauth2/authorize?{qs}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
