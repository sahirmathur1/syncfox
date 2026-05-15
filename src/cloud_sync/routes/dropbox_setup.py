"""Dropbox OAuth setup routes — Phase 5 (Syncfox).

  GET  /remotes/connect/dropbox            → if app creds configured, kick OAuth;
                                              else render in-UI form
  POST /remotes/connect/dropbox/configure  → save app key/secret to DB → redirect to OAuth init
  GET  /remotes/oauth/callback/dropbox     → exchange code for tokens

App credentials (client_id + client_secret) can come from either:
  - .env (DROPBOX_OAUTH_CLIENT_ID + DROPBOX_OAUTH_CLIENT_SECRET) — back-compat
  - app_settings table (set via the form) — new in this patch

DB-stored secret is encrypted with the same Fernet path as remote configs
when SYNCFOX_MASTER_PASSWORD is set.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_sync.persistence import repos
from cloud_sync.providers import dropbox as dropbox_provider
from cloud_sync.sync import conf_writer, encryption

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Per-state-token sessions: state → {verifier, account_label}
_SESSIONS: dict[str, dict] = {}

# app_settings keys
_KEY_CLIENT_ID = "dropbox_oauth_client_id"
_KEY_CLIENT_SECRET = "dropbox_oauth_client_secret"


async def _resolve_app_creds(db, settings) -> tuple[str | None, str | None]:
    """Returns (client_id, client_secret), preferring DB values over env.

    Either may be None if not configured. Secret is decrypted on read."""
    db_id = repos.get_app_setting(db, _KEY_CLIENT_ID)
    db_secret_blob = repos.get_app_setting(db, _KEY_CLIENT_SECRET)
    db_secret = await encryption.decrypt(db_secret_blob) if db_secret_blob else None

    client_id = db_id or settings.dropbox_oauth_client_id
    client_secret = db_secret or settings.dropbox_oauth_client_secret
    return client_id, client_secret


def _render_form(client_id: str | None, redirect_uri: str) -> HTMLResponse:
    page = (_STATIC_DIR / "dropbox_form.html").read_text()
    has_existing = bool(client_id)
    page = (page
            .replace("{{REDIRECT_URI}}", redirect_uri)
            .replace("{{CLIENT_ID}}", client_id or "")
            .replace("{{SECRET_REQUIRED}}", "" if has_existing else "required")
            .replace("{{SECRET_PLACEHOLDER}}",
                     "leave blank to keep the saved secret" if has_existing
                     else "paste app secret"))
    return HTMLResponse(page)


@router.get("/remotes/connect/dropbox", response_model=None)
async def initiate(request: Request) -> RedirectResponse | HTMLResponse:
    settings = request.app.state.settings
    db = request.app.state.db
    client_id, client_secret = await _resolve_app_creds(db, settings)

    # If app creds aren't fully configured, or operator forced a re-config,
    # show the form instead of kicking OAuth.
    force = request.query_params.get("force") == "1"
    if force or not client_id or not client_secret:
        return _render_form(client_id, settings.dropbox_oauth_redirect_url)

    verifier, challenge = dropbox_provider.make_pkce_pair()
    state = secrets.token_urlsafe(24)
    _SESSIONS[state] = {"verifier": verifier}
    url = dropbox_provider.authorize_url(
        client_id=client_id,
        redirect_uri=settings.dropbox_oauth_redirect_url,
        code_challenge=challenge,
        state=state,
    )
    logger.info("dropbox: redirecting to OAuth (state=%s)", state[:8])
    return RedirectResponse(url, status_code=302)


@router.post("/remotes/connect/dropbox/configure", response_model=None)
async def configure(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    settings = request.app.state.settings
    db = request.app.state.db

    client_id = client_id.strip()
    client_secret = client_secret.strip()

    if not client_id:
        raise HTTPException(400, "client_id required")

    # If the operator left the secret blank, keep the existing one. Useful
    # for editing the client_id without re-typing the secret.
    existing_secret_blob = repos.get_app_setting(db, _KEY_CLIENT_SECRET)
    if not client_secret:
        if not existing_secret_blob:
            raise HTTPException(400, "client_secret required (no saved secret to reuse)")
    else:
        encrypted = await encryption.encrypt(client_secret)
        repos.set_app_setting(db, _KEY_CLIENT_SECRET, encrypted)

    repos.set_app_setting(db, _KEY_CLIENT_ID, client_id)
    logger.info("dropbox: app creds saved (client_id=%s…)", client_id[:6])

    # Fall through to OAuth init.
    return RedirectResponse("/remotes/connect/dropbox", status_code=302)


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

    client_id, client_secret = await _resolve_app_creds(db, settings)
    if not client_id or not client_secret:
        raise HTTPException(500, "Dropbox app credentials missing at callback — "
                                  "go to /remotes/connect/dropbox?force=1 to reconfigure")

    # Exchange the authorization code + verifier for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.dropbox.com/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
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
        client_id=client_id,
        client_secret=client_secret,
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
