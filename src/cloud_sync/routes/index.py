from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from cloud_sync.routes.setup import setup_state

router = APIRouter()


@router.get("/")
async def index(request: Request) -> RedirectResponse:
    """Phase 3 (Syncfox): redirect to /setup until the user has at least
    one pair, otherwise to /pairs. Keeps fresh installs from landing on
    an empty /pairs and being confused."""
    db = request.app.state.db
    state = setup_state(db)
    if state < 3:
        return RedirectResponse("/setup", status_code=302)
    return RedirectResponse("/pairs", status_code=302)
