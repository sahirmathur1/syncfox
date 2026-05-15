"""Phase 5 (Syncfox) — Dropbox provider helper tests."""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

from cloud_sync.providers import dropbox as dropbox_provider


def test_make_pkce_pair_round_trip() -> None:
    verifier, challenge = dropbox_provider.make_pkce_pair()
    # Verifier: 43-128 unreserved chars per RFC 7636
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    # Challenge = base64url(sha256(verifier)).strip(=)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_authorize_url_carries_required_params() -> None:
    url = dropbox_provider.authorize_url(
        client_id="abc123",
        redirect_uri="http://localhost:8081/cb",
        code_challenge="testchallenge",
        state="teststate",
    )
    p = urlparse(url)
    assert p.netloc == "www.dropbox.com"
    assert p.path == "/oauth2/authorize"
    qs = parse_qs(p.query)
    assert qs["client_id"] == ["abc123"]
    assert qs["response_type"] == ["code"]
    assert qs["token_access_type"] == ["offline"]  # gives us a refresh_token
    assert qs["code_challenge"] == ["testchallenge"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == ["teststate"]
    assert qs["redirect_uri"] == ["http://localhost:8081/cb"]


def test_credentials_round_trip() -> None:
    c = dropbox_provider.DropboxCredentials(
        client_id="cid",
        client_secret="cs",
        access_token="at",
        refresh_token="rt",
        expiry_iso="2026-06-01T00:00:00Z",
        account_email="x@y.com",
    )
    blob = dropbox_provider.serialize(c)
    back = dropbox_provider.deserialize(blob)
    assert back == c


def test_credentials_deserialize_handles_missing_optional_fields() -> None:
    """Older blobs (pre-revoked_at) should still load."""
    minimal = '{"client_id":"a","client_secret":"b","access_token":"c","refresh_token":"d","expiry_iso":"2026-01-01T00:00:00Z"}'
    back = dropbox_provider.deserialize(minimal)
    assert back.account_email == ""
    assert back.revoked_at == ""
