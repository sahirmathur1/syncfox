"""Two-step interactive iCloud setup using rclone's --state/--continue protocol.

   GET  /remotes/connect/icloud         → form: Apple ID + password
   POST /remotes/connect/icloud         → walks rclone config state machine
                                          through service/apple_id/password/
                                          advanced prompts; rclone then asks
                                          for the 2FA code → we save the
                                          state token and render the 2FA form
   GET  /remotes/connect/icloud/2fa     → (the form is rendered above; this
                                          handler exists for back-button)
   POST /remotes/connect/icloud/2fa     → continues with the code, walks any
                                          remaining prompts, persists the
                                          rclone-config stanza
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
from configparser import ConfigParser
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_sync.persistence import repos
from cloud_sync.providers import icloud as icloud_provider
from cloud_sync.sync import conf_writer, encryption, rclone

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Active sessions: sid → dict(state, apple_id, password_obscured, remote_name)
_SESSIONS: dict[str, dict] = {}


### -------------- helpers --------------


async def _rclone_step(remote_name: str, *, apple_id: str, password_obscured: str,
                       state: str | None, result: str | None) -> dict:
    """Invoke `rclone config create ... --all --non-interactive [--continue ...]`
    and return the parsed JSON response. Empty string ResultStr / no Option =
    state machine terminated successfully."""
    args = [
        "/usr/bin/rclone", "--config", "/data/rclone.conf",
        "config", "create", remote_name, "iclouddrive",
        f"apple_id={apple_id}", f"password={password_obscured}",
        "--all", "--non-interactive",
    ]
    if state is not None:
        args.extend(["--continue", "--state", state, "--result", result or ""])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    out_s = out.decode(errors="replace")
    err_s = err.decode(errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(f"rclone exit {proc.returncode}: stderr={err_s.strip()!r}")

    # rclone prints the JSON describing the next prompt. If state machine ended,
    # output may be empty or a final-state JSON.
    out_s = out_s.strip()
    if not out_s:
        return {"State": "", "Option": None, "Error": "", "Result": ""}
    try:
        return json.loads(out_s)
    except json.JSONDecodeError:
        # rclone occasionally prints a leading log NOTICE before JSON; strip
        m = re.search(r'\{.*\}', out_s, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f"could not parse rclone JSON: {out_s[:300]!r}")


### -------------- GET form for credentials --------------


@router.get("/remotes/connect/icloud")
async def form() -> HTMLResponse:
    return HTMLResponse((_STATIC_DIR / "icloud_form.html").read_text())


### -------------- POST credentials → walk to 2FA prompt --------------


@router.post("/remotes/connect/icloud")
async def submit_credentials(request: Request,
                             apple_id: str = Form(...),
                             password: str = Form(...)) -> HTMLResponse:
    if "@" not in apple_id:
        raise HTTPException(400, "apple_id must be an email address")
    if len(password) < 6:
        raise HTTPException(400, "password too short")

    safe = re.sub(r"[^a-z0-9]+", "-", apple_id.split("@")[0].lower()).strip("-")
    remote_name = f"icloud-{safe}" if safe else "icloud-default"
    obs = await rclone.obscure(password)

    # Walk the state machine:
    #   step 0: initial — returns service prompt
    #   step 1: result=drive → apple_id prompt
    #   step 2: result=<apple_id> → password prompt
    #   step 3: result=<password_obscured> → advanced prompt
    #   step 4: result=false → AUTH attempt, then 2FA code prompt
    state = None
    result = None
    last_response: dict = {}
    for step_idx, want_value in enumerate([
        None,                # initial
        "drive",             # service
        apple_id,            # apple_id
        obs,                 # password (must be the obscured form)
        "false",             # advanced
    ]):
        if step_idx == 0:
            try:
                last_response = await _rclone_step(
                    remote_name, apple_id=apple_id, password_obscured=obs,
                    state=None, result=None,
                )
            except RuntimeError as e:
                raise HTTPException(400, f"iCloud auth failed at start: {e}") from e
            state = last_response.get("State")
            continue

        try:
            last_response = await _rclone_step(
                remote_name, apple_id=apple_id, password_obscured=obs,
                state=state, result=want_value,
            )
        except RuntimeError as e:
            # Most likely failure: bad password → "authSRPComplete: sign in failed"
            msg = str(e)
            if "incorrect username or password" in msg.lower() or "sign in failed" in msg.lower():
                raise HTTPException(400, "Apple rejected the credentials — double-check your Apple ID and password (NOT an app-specific password).")
            raise HTTPException(400, f"iCloud setup failed at step {step_idx}: {msg}") from e

        state = last_response.get("State", "")
        opt = last_response.get("Option") or {}
        opt_name = opt.get("Name", "")
        # If we hit a 2FA prompt early (some accounts skip the advanced step),
        # bail out of the loop now.
        if "2fa" in opt_name.lower() or "code" in (opt.get("Help") or "").lower():
            break

    # We should now be at a 2FA prompt
    opt = last_response.get("Option") or {}
    if not opt:
        raise HTTPException(500, f"rclone state-machine ended early — last response: {json.dumps(last_response)[:300]}")
    help_text = (opt.get("Help") or "").lower()
    name = (opt.get("Name") or "").lower()
    if "2fa" not in name and "code" not in help_text and "verification" not in help_text:
        raise HTTPException(500, f"expected 2FA prompt, got: name={opt.get('Name')!r} help={opt.get('Help')!r}")

    sid = secrets.token_urlsafe(16)
    _SESSIONS[sid] = {
        "state": state,
        "apple_id": apple_id,
        "password_obscured": obs,
        "remote_name": remote_name,
    }
    logger.info("iCloud session %s waiting for 2FA code (%s, remote=%s)",
                sid[:8], apple_id, remote_name)
    return HTMLResponse(
        (_STATIC_DIR / "icloud_2fa.html").read_text().replace("{{SID}}", sid)
    )


### -------------- POST re-auth (re-uses stored creds, jumps straight to 2FA) ----


@router.post("/remotes/{remote_name}/reauth")
async def reauth(remote_name: str, request: Request) -> HTMLResponse:
    """Phase 1 of Syncfox — kick off a fresh iCloud auth using the stored
    Apple ID + obscured password. Skips the credentials form (operator
    already provided them at first-time setup); walks the rclone state
    machine to the 2FA prompt; renders the existing 2FA form.

    Reusable for "trust token expired" and "operator wants to refresh
    proactively" — both look the same to Apple.
    """
    settings = request.app.state.settings
    db = request.app.state.db

    row = repos.get_remote(db, remote_name)
    if row is None:
        raise HTTPException(404, f"remote {remote_name!r} not found")
    if row["provider"] != "icloud":
        raise HTTPException(400, f"remote {remote_name!r} is not an iCloud remote")

    blob = await encryption.decrypt(row["encrypted_config"], settings.age_secret_key)
    creds = icloud_provider.deserialize(blob)
    if not creds.password_obscured:
        raise HTTPException(
            400,
            f"remote {remote_name!r} has no stored password — re-add via /remotes/connect/icloud",
        )

    # Walk the same state machine as first-time setup, but skip the form.
    apple_id = creds.apple_id
    obs = creds.password_obscured

    state = None
    last_response: dict = {}
    for step_idx, want_value in enumerate([
        None,                # initial
        "drive",             # service
        apple_id,            # apple_id
        obs,                 # password
        "false",             # advanced
    ]):
        if step_idx == 0:
            try:
                last_response = await _rclone_step(
                    remote_name, apple_id=apple_id, password_obscured=obs,
                    state=None, result=None,
                )
            except RuntimeError as e:
                raise HTTPException(400, f"iCloud reauth failed at start: {e}") from e
            state = last_response.get("State")
            continue

        try:
            last_response = await _rclone_step(
                remote_name, apple_id=apple_id, password_obscured=obs,
                state=state, result=want_value,
            )
        except RuntimeError as e:
            raise HTTPException(400, f"iCloud reauth failed at step {step_idx}: {e}") from e

        state = last_response.get("State", "")
        opt = last_response.get("Option") or {}
        opt_name = opt.get("Name", "")
        if "2fa" in opt_name.lower() or "code" in (opt.get("Help") or "").lower():
            break

    opt = last_response.get("Option") or {}
    if not opt:
        raise HTTPException(500, f"rclone state-machine ended early: {json.dumps(last_response)[:300]}")

    sid = secrets.token_urlsafe(16)
    _SESSIONS[sid] = {
        "state": state,
        "apple_id": apple_id,
        "password_obscured": obs,
        "remote_name": remote_name,
    }
    logger.info("iCloud REAUTH session %s waiting for 2FA code (%s, remote=%s)",
                sid[:8], apple_id, remote_name)
    return HTMLResponse(
        (_STATIC_DIR / "icloud_2fa.html").read_text().replace("{{SID}}", sid)
    )


### -------------- POST 2FA code → finish auth, persist --------------


@router.post("/remotes/connect/icloud/2fa")
async def submit_2fa(request: Request,
                     sid: str = Form(...),
                     code: str = Form(...)) -> RedirectResponse:
    settings = request.app.state.settings
    db = request.app.state.db

    sess = _SESSIONS.pop(sid, None)
    if sess is None:
        raise HTTPException(400, "session not found or expired — restart from /remotes/connect/icloud")

    cleaned = re.sub(r"\s|-", "", code).strip()
    if not re.fullmatch(r"\d{6}", cleaned):
        _SESSIONS[sid] = sess
        raise HTTPException(400, "code must be 6 digits")

    # Continue the state machine with the 2FA code; loop through any remaining
    # prompts (e.g. "Trust this device?") accepting defaults until terminal.
    state = sess["state"]
    next_result = cleaned
    last_response: dict = {}
    for _ in range(8):
        try:
            last_response = await _rclone_step(
                sess["remote_name"],
                apple_id=sess["apple_id"],
                password_obscured=sess["password_obscured"],
                state=state, result=next_result,
            )
        except RuntimeError as e:
            raise HTTPException(400, f"2FA verification failed: {e}") from e

        opt = last_response.get("Option")
        state = last_response.get("State", "")
        if not opt:
            # state machine done
            break
        # Accept whatever the default is for any subsequent prompt
        next_result = opt.get("ValueStr", "") or opt.get("DefaultStr", "")

    # Read the stanza rclone wrote
    conf_path = settings.data_dir / "rclone.conf"
    cfg = ConfigParser()
    cfg.read(conf_path)
    remote_name = sess["remote_name"]
    if remote_name not in cfg:
        raise HTTPException(500, f"rclone reported success but {remote_name!r} not in /data/rclone.conf")
    stanza = dict(cfg[remote_name])

    creds = icloud_provider.ICloudCredentials(
        apple_id=sess["apple_id"],
        password_obscured=sess["password_obscured"],
        trust_token=stanza.get("trust_token", ""),
        cookies=stanza.get("cookies", ""),
    )
    encrypted = await encryption.encrypt(
        icloud_provider.serialize(creds), settings.age_secret_key,
    )
    repos.upsert_remote(
        db,
        name=remote_name,
        provider="icloud",
        account_label=sess["apple_id"],
        encrypted_config=encrypted,
    )
    await conf_writer.rebuild(
        db,
        conf_path=conf_path,
        age_secret_key=settings.age_secret_key,
        credentials_dir=settings.data_dir / "credentials",
    )
    logger.info("connected iCloud %s as remote %r", sess["apple_id"], remote_name)
    return RedirectResponse(f"/remotes?ok={remote_name}", status_code=302)
