"""/remotes — list connected accounts, Google SA-key upload, iCloud add."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_sync.persistence import repos
from cloud_sync.providers import expiry as expiry_provider
from cloud_sync.providers import google as google_provider
from cloud_sync.providers import icloud as icloud_provider
from cloud_sync.sync import conf_writer, encryption, rclone

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


### -------------- /remotes (list) --------------


@router.get("/remotes")
async def remotes_index(request: Request) -> HTMLResponse:
    db = request.app.state.db
    settings = request.app.state.settings
    rows = repos.list_remotes(db)
    rows_html = ""
    if not rows:
        rows_html = '<tr><td colspan="5" class="muted">No accounts connected yet.</td></tr>'
    else:
        for r in rows:
            # Phase 1 (Syncfox) — token-age badge for providers we track
            # expiry on (today: iCloud only).
            status = expiry_provider.status_for(
                r["provider"], r["last_verified_at"],
                orange_days=settings.syncfox_badge_orange_days,
                red_days=settings.syncfox_badge_red_days,
            )
            badge_html = (
                f'<span class="badge badge-{status.color}">{status.label}</span>'
                if status.color != "neutral"
                else f'<span class="badge badge-neutral">—</span>'
            )

            # Phase 1 (Syncfox) — Re-authenticate button for iCloud only.
            actions = []
            if r["provider"] == "icloud":
                actions.append(
                    f'<form method="post" action="/remotes/{r["name"]}/reauth" style="margin:0">'
                    f'<button type="submit" class="btn-warn">Re-authenticate</button>'
                    f'</form>'
                )
            actions.append(
                f'<form method="post" action="/remotes/{r["name"]}/delete" style="margin:0" '
                f'onsubmit="return confirm(\'Delete remote {r["name"]}? Pairs using it will break.\')">'
                f'<button class="btn-danger">Delete</button></form>'
            )
            rows_html += (
                f'<tr><td>{r["name"]}</td><td>{r["provider"]}</td>'
                f'<td>{r["account_label"] or "—"}</td>'
                f'<td>{badge_html}</td>'
                f'<td><div class="actions">{"".join(actions)}</div></td></tr>'
            )
    page = (_STATIC_DIR / "remotes.html").read_text()
    return HTMLResponse(page.replace("{{ROWS}}", rows_html))


### -------------- Google SA key upload --------------


@router.get("/remotes/connect/google")
async def connect_google_form(request: Request) -> HTMLResponse:
    return HTMLResponse((_STATIC_DIR / "google_form.html").read_text())


@router.post("/remotes/connect/google")
async def connect_google_submit(request: Request,
                                name_hint: str = Form(""),
                                key_file: UploadFile = File(...),
                                team_drive_id: str = Form("")) -> RedirectResponse:
    settings = request.app.state.settings
    db = request.app.state.db

    raw = (await key_file.read()).decode("utf-8", errors="replace")
    try:
        sa = google_provider.parse_key_file(raw)
    except ValueError as e:
        raise HTTPException(400, f"invalid SA key: {e}")

    # Verify the key actually works against Drive
    try:
        about = await google_provider.test_connect(sa)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Drive test-connect failed: {type(e).__name__}: {e}")

    # Pick a stable remote name: gdrive-<sa-username>, optionally with a hint suffix
    sa_username = sa.client_email.split("@")[0]
    base = re.sub(r"[^a-z0-9]+", "-", sa_username.lower()).strip("-")
    suffix = re.sub(r"[^a-z0-9]+", "-", name_hint.lower()).strip("-") if name_hint.strip() else ""
    remote_name = f"gdrive-{base}" + (f"-{suffix}" if suffix else "")

    encrypted = await encryption.encrypt(
        google_provider.serialize(sa), settings.age_secret_key,
    )
    repos.upsert_remote(
        db,
        name=remote_name,
        provider="google",
        account_label=sa.client_email,
        encrypted_config=encrypted,
    )
    await conf_writer.rebuild(
        db,
        conf_path=settings.data_dir / "rclone.conf",
        age_secret_key=settings.age_secret_key,
        credentials_dir=settings.data_dir / "credentials",
    )
    logger.info("connected google SA %s as remote %r", sa.client_email, remote_name)
    return RedirectResponse(f"/remotes?ok={remote_name}", status_code=302)


### -------------- iCloud routes live in routes/icloud_setup.py --------------


### -------------- delete --------------


@router.post("/remotes/{name}/delete")
async def delete(name: str, request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    db = request.app.state.db
    repos.delete_remote(db, name)
    await conf_writer.rebuild(
        db,
        conf_path=settings.data_dir / "rclone.conf",
        age_secret_key=settings.age_secret_key,
        credentials_dir=settings.data_dir / "credentials",
    )
    return RedirectResponse("/remotes", status_code=302)
