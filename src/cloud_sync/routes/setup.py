"""/setup — first-run onboarding wizard.

Phase 3 of Syncfox. The wizard is a single render that progresses based
on what's already in the DB:

  state 0: no remotes  → step 1 active, prompt to connect first provider
  state 1: 1 remote    → step 2 active, prompt to connect a second
  state 2: ≥2 remotes,
           no pairs    → step 3 active, link to /pairs/new
  state 3: ≥1 pair     → wizard "done", redirect to /pairs

The middleware in main.py redirects `/` to `/setup` while in states 0-2,
otherwise to `/pairs`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_sync.persistence import repos

router = APIRouter()
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def setup_state(db) -> int:
    """0/1/2/3 — see module docstring."""
    n_remotes = len(repos.list_remotes(db))
    n_pairs = len(repos.list_pairs(db))
    if n_pairs > 0:
        return 3
    if n_remotes >= 2:
        return 2
    if n_remotes == 1:
        return 1
    return 0


_PROVIDER_TILES = """
<div class="providers">
  <a class="provider" href="/remotes/connect/google">
    <div class="badge">Provider</div>
    <strong>Google Drive</strong>
    <span>Service-account key OR OAuth (paste a JSON key for now; OAuth UI in a follow-up).</span>
  </a>
  <a class="provider" href="/remotes/connect/icloud">
    <div class="badge">Provider</div>
    <strong>iCloud Drive</strong>
    <span>Apple ID + 2FA. Trust token good for ~30 days; Syncfox tracks it for you.</span>
  </a>
  <a class="provider" href="/remotes/connect/dropbox">
    <div class="badge">Provider</div>
    <strong>Dropbox</strong>
    <span>PKCE OAuth flow. Refresh tokens don't expire by default — set + forget.</span>
  </a>
</div>
"""


def _render_active_block(state: int) -> str:
    if state == 0:
        return f"""<h3 style="margin-top:8px">Connect your first cloud account</h3>{_PROVIDER_TILES}
        <p class="muted" style="margin-top:14px;font-size:12px">Or skip the wizard and go straight to <a href="/remotes" style="color:var(--accent)">/remotes</a>.</p>"""
    if state == 1:
        return f"""<h3 style="margin-top:8px">Connect one more — you need two endpoints to sync</h3>{_PROVIDER_TILES}
        <p class="muted" style="margin-top:14px;font-size:12px">Already have what you need? Skip ahead to <a href="/pairs" style="color:var(--accent)">/pairs</a>.</p>"""
    # state 2
    return """<h3 style="margin-top:8px">Now create your first pair</h3>
    <p>You've got two cloud accounts wired up. Pick a source path on one, a destination path on the other, choose how often Syncfox polls for changes, and you're done.</p>
    <div class="cta-row">
      <a class="cta" href="/pairs/new">Create pair →</a>
      <a class="cta-secondary" href="/remotes">Add another account first</a>
    </div>"""


@router.get("/setup", response_model=None)
async def setup(request: Request) -> HTMLResponse | RedirectResponse:
    db = request.app.state.db
    state = setup_state(db)
    if state == 3:
        return RedirectResponse("/pairs", status_code=302)

    page = (_STATIC_DIR / "setup.html").read_text()
    classes = ["", "", ""]
    blurbs = [
        "Pick a provider tile below — Syncfox walks you through OAuth or 2FA. Your credentials never leave this container.",
        "You connected one. Add a second so there's somewhere to sync to.",
        "Two accounts connected. Final step.",
    ]
    classes[state] = "active"
    for i in range(state):
        classes[i] = "done"
    page = (page
            .replace("{{STEP1_CLASS}}", classes[0])
            .replace("{{STEP2_CLASS}}", classes[1])
            .replace("{{STEP3_CLASS}}", classes[2])
            .replace("{{STEP1_BLURB}}", blurbs[0])
            .replace("{{ACTIVE_BLOCK}}", _render_active_block(state)))
    return HTMLResponse(page)
