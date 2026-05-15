"""Dropbox OAuth setup routes — Phase 5 (Syncfox).

  GET  /remotes/connect/dropbox            → kick off OAuth (no form)
  GET  /remotes/oauth/callback/dropbox     → exchange code for tokens
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_sync.persistence import repos
from cloud_sync.providers import dropbox as dropbox_provider
from cloud_sync.sync import conf_writer, encryption

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Per-state-token sessions: state → {verifier, account_label}
_SESSIONS: dict[str, dict] = {}


@router.get("/remotes/connect/dropbox", response_model=None)
async def initiate(request: Request) -> RedirectResponse | HTMLResponse:
    settings = request.app.state.settings
    if not settings.dropbox_oauth_client_id:
        return HTMLResponse(
            "<h1>Dropbox setup needed</h1>"
            "<p>Set <code>DROPBOX_OAUTH_CLIENT_ID</code> + "
            "<code>DROPBOX_OAUTH_CLIENT_SECRET</code> in your <code>.env</code>, "
            "then restart Syncfox. Create the app at "
            "<a href='https://www.dropbox.com/developers/apps' target='_blank'>"
            "dropbox.com/developers/apps</a>; redirect URI = "
            f"<code>{settings.dropbox_oauth_redirect_url}</code>.</p>",
            status_code=503,
        )

    verifier, challenge = dropbox_provider.make_pkce_pair()
    state = secrets.token_urlsafe(24)
    _SESSIONS[state] = {"verifier": verifier}
    url = dropbox_provider.authorize_url(
        client_id=settings.dropbox_oauth_client_id,
        redirect_uri=settings.dropbox_oauth_redirect_url,
        code_challenge=challenge,
        state=state,
    )
    logger.info("dropbox: redirecting to OAuth (state=%s)", state[:8])
    return RedirectResponse(url, status_code=302)


@router.get("/remotes/oauth/callback/dropbox", response_model=None)
async def callback(request: Request) -> RedirectResponse | HTMLResponse:
    settings = request.app.state.settings
    db = request.app.state.db

    code = request.query_params.get("code") or ""
    state = request.query_params.get("state") or ""
    err = request.query_params.get("error") or ""

    if err:
        return HTMLResponse(
            f"<h1>Dropbox returned an error</h1><p>{err}: "
            f"{request.query_params.get('error_description', '')}</p>"
            f"<p><a href='/remotes'>Back</a></p>",
            status_code=400,
        )

    sess = _SESSIONS.pop(state, None)
    if sess is None:
        raise HTTPException(400, "state token not recognized — restart from /remotes/connect/dropbox")
    if not code:
        raise HTTPException(400, "missing ?code= param from Dropbox")

    # Exchange the authorization code + verifier for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.dropbox.com/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": settings.dropbox_oauth_client_id,
                "client_secret": settings.dropbox_oauth_client_secret,
                "redirect_uri": settings.dropbox_oauth_redirect_url,
                "code_verifier": sess["verifier"],
            },
        )
    if r.status_code != 200:
        raise HTTPException(400, f"Dropbox token exchange failed ({r.status_code}): {r.text[:300]}")
    token = r.json()
    access = token["access_token"]
    refresh = token.get("refresh_token", "")
    expires_in = int(token.get("expires_in", 14400))
    expiry_iso = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pull account info so we can label the row in /remotes
    account_email = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            who = await client.post(
                "https://api.dropboxapi.com/2/users/get_current_account",
                headers={"Authorization": f"Bearer {access}"},
            )
        if who.status_code == 200:
            account_email = who.json().get("email", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("dropbox get_current_account failed: %s", e)

    safe = re.sub(r"[^a-z0-9]+", "-", (account_email.split("@")[0] or "default").lower()).strip("-")
    remote_name = f"dropbox-{safe}"

    creds = dropbox_provider.DropboxCredentials(
        client_id=settings.dropbox_oauth_client_id,
        client_secret=settings.dropbox_oauth_client_secret,
        access_token=access,
        refresh_token=refresh,
        expiry_iso=expiry_iso,
        account_email=account_email,
    )
    encrypted = await encryption.encrypt(
        dropbox_provider.serialize(creds), settings.age_secret_key,
    )
    repos.upsert_remote(
        db,
        name=remote_name,
        provider="dropbox",
        account_label=account_email or "(unknown account)",
        encrypted_config=encrypted,
    )
    await conf_writer.rebuild(
        db,
        conf_path=settings.data_dir / "rclone.conf",
        age_secret_key=settings.age_secret_key,
        credentials_dir=settings.data_dir / "credentials",
    )
    logger.info("dropbox: connected %s as %r", account_email, remote_name)
    return RedirectResponse(f"/remotes?ok={remote_name}", status_code=302)
