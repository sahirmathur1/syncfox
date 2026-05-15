from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cloud_sync.sync import rclone

router = APIRouter()


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Deeper liveness — exercises rclone + DB."""
    db = request.app.state.db
    db.execute("select 1").fetchone()
    rclone_version = await rclone.version()
    return JSONResponse({
        "status": "ready",
        "rclone": rclone_version,
        "remotes": db.execute("select count(*) from rclone_remotes").fetchone()[0],
        "pairs": db.execute("select count(*) from pairs").fetchone()[0],
    })
