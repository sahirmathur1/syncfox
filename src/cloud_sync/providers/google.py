"""Google Drive provider — service-account-key auth.

Sahir explicitly chose service accounts over OAuth (2026-05-10) — see the
"Service account vs OAuth" comparison in the Roadmap note. The user uploads
the SA JSON key once; we validate it (call drive.about.get), persist it
encrypted, and rclone uses `service_account_file=/data/credentials/<name>.json`
on every sync.

The SA accesses only folders explicitly shared with its email
(<sa-name>@<project>.iam.gserviceaccount.com). For Workspace shared drives,
the SA is added as a Member.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass(frozen=True, slots=True)
class GoogleSAKey:
    """The minimum subset of fields we keep from the JSON key."""
    client_email: str  # e.g. cloud-sync-sa@pulseyeg-tembo.iam.gserviceaccount.com
    project_id: str
    raw_json: str      # the entire original JSON blob, kept verbatim for rclone


def parse_key_file(blob: str) -> GoogleSAKey:
    """Validate that a blob is a Google SA key JSON. Raises ValueError on bad shape."""
    try:
        d = json.loads(blob)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: {e}") from e
    if d.get("type") != "service_account":
        raise ValueError(f"not a service-account key (type={d.get('type')!r})")
    for required in ("client_email", "private_key", "project_id", "private_key_id"):
        if required not in d:
            raise ValueError(f"missing required field {required!r}")
    return GoogleSAKey(
        client_email=d["client_email"],
        project_id=d["project_id"],
        raw_json=blob,
    )


async def test_connect(key: GoogleSAKey) -> dict:
    """Hit Drive's `about.get` with the SA credentials to confirm the key is
    live + has Drive access. Returns the about response on success."""
    creds = service_account.Credentials.from_service_account_info(
        json.loads(key.raw_json),
        scopes=[DRIVE_SCOPE],
    )
    creds.refresh(Request())  # blocks; fine — short call
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(
            "https://www.googleapis.com/drive/v3/about",
            params={"fields": "user(emailAddress,displayName),storageQuota"},
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        r.raise_for_status()
        return r.json()


def serialize(key: GoogleSAKey) -> str:
    """We persist the original JSON blob verbatim so rclone can read it."""
    return key.raw_json


def deserialize(blob: str) -> GoogleSAKey:
    return parse_key_file(blob)
