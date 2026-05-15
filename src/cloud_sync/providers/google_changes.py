"""Google Drive `changes.list` watermark polling.

We use the SA's saved JSON key to get an access token, then hit the Drive
v3 changes API. Two functions:

  start_page_token() — once per pair, called the first time we observe
                       a Google-source pair; returns the current head of
                       the change journal
  changed_since(tok) — every poll cycle; returns (new_token, count_of_changes)

`shared_with_me` mode is mandatory for SA-authed remotes — otherwise the
SA's "My Drive" (empty) is what changes.list reports against, and we'd
miss every actual change.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _credentials_for(sa_key_path: Path) -> service_account.Credentials:
    creds = service_account.Credentials.from_service_account_file(
        str(sa_key_path), scopes=[DRIVE_SCOPE],
    )
    creds.refresh(Request())
    return creds


async def start_page_token(sa_key_path: Path) -> str:
    """Initial seed — return the current head of the change journal."""
    creds = _credentials_for(sa_key_path)
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(
            "https://www.googleapis.com/drive/v3/changes/startPageToken",
            params={"supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true"},
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        r.raise_for_status()
        return r.json()["startPageToken"]


async def changed_since(sa_key_path: Path, page_token: str) -> tuple[str, int]:
    """Walk the change journal from page_token to the latest. Returns
    (new_token, total_changes_seen). new_token == page_token if nothing
    changed."""
    creds = _credentials_for(sa_key_path)
    seen = 0
    next_token: str | None = page_token
    new_start_token: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as c:
        while next_token:
            r = await c.get(
                "https://www.googleapis.com/drive/v3/changes",
                params={
                    "pageToken": next_token,
                    "spaces": "drive",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    # Limit fields we actually care about to keep payload tiny
                    "fields": "newStartPageToken,nextPageToken,changes(fileId,removed,time)",
                    "pageSize": "500",
                },
                headers={"Authorization": f"Bearer {creds.token}"},
            )
            if r.status_code == 410:
                # Token gone (>30 days idle, or manually invalidated) — caller
                # should refetch via start_page_token() and trigger a defensive
                # bisync.
                raise TokenExpired("drive page token rejected (410 Gone)")
            r.raise_for_status()
            body = r.json()
            seen += len(body.get("changes") or [])
            if "newStartPageToken" in body:
                new_start_token = body["newStartPageToken"]
                break
            next_token = body.get("nextPageToken")
        if new_start_token is None:
            new_start_token = page_token
    return new_start_token, seen


class TokenExpired(RuntimeError):
    """Raised when Drive returns 410 for our pageToken."""
