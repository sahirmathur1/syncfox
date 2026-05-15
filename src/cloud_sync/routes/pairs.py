"""/pairs — list, create, run, resync."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from cloud_sync.persistence import repos
from cloud_sync.sync import rclone
from cloud_sync.sync.watchers import WatcherManager

logger = logging.getLogger(__name__)
router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Per-pair flock — ensures only one bisync at a time per pair.
_PAIR_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(pair_id: str) -> asyncio.Lock:
    lock = _PAIR_LOCKS.get(pair_id)
    if lock is None:
        lock = asyncio.Lock()
        _PAIR_LOCKS[pair_id] = lock
    return lock


### -------------- /pairs list --------------


@router.get("/pairs")
async def pairs_index(request: Request) -> HTMLResponse:
    db = request.app.state.db
    rows = repos.list_pairs(db)
    rows_html = ""
    if not rows:
        rows_html = '<tr><td colspan="6" class="muted">No pairs yet. <a href="/pairs/new">Create one</a>.</td></tr>'
    else:
        for r in rows:
            status = "🟢" if r["last_success_at"] and (r["last_failure_at"] is None or r["last_success_at"] > r["last_failure_at"]) else ("🔴" if r["last_failure_at"] else "—")
            rows_html += (
                f'<tr><td>{status}</td><td><a href="/pairs/{r["id"]}">{r["name"]}</a></td>'
                f'<td>{r["source_remote"]}:{r["source_path"]} → {r["destination_remote"]}:{r["destination_path"]}</td>'
                f'<td>{r["last_success_at"] or "never"}</td>'
                f'<td>{"paused" if r["paused"] else "active"}</td>'
                f'<td>'
                f'<form method="post" action="/pairs/{r["id"]}/run" style="display:inline"><button>Run</button></form> '
                f'</td></tr>'
            )
    page = (_STATIC_DIR / "pairs.html").read_text()
    return HTMLResponse(page.replace("{{ROWS}}", rows_html))


async def trigger_run(app, pair_id: str, *, trigger: str = "poll-detected",
                      resync: bool = False) -> None:
    """Module-level async trigger usable by both routes and watchers.

    Acquires the pair's flock, runs bisync, persists run row + outcome.
    Re-entrant safe: if the pair is already running, this returns
    immediately (the watcher's tick is dropped).
    """
    db = app.state.db
    settings = app.state.settings
    pair = repos.get_pair(db, pair_id)
    if pair is None or pair["paused"]:
        return
    lock = _lock_for(pair_id)
    if lock.locked():
        return  # already running

    async with lock:
        run_id = repos.insert_run(db, pair_id=pair_id, trigger=trigger)
        log_path = settings.data_dir / "logs" / f"pair-{pair_id}-{run_id}.log"
        filters_path = None
        if pair["filters"].strip():
            filters_path = settings.data_dir / "tmp" / f"pair-{pair_id}-filters.txt"
            filters_path.parent.mkdir(parents=True, exist_ok=True)
            filters_path.write_text(pair["filters"])

        source = f'{pair["source_remote"]}:{pair["source_path"]}'
        destination = f'{pair["destination_remote"]}:{pair["destination_path"]}'
        try:
            result = await rclone.bisync(
                source=source, destination=destination,
                work_dir=settings.data_dir / "work" / f"pair-{pair_id}",
                log_path=log_path,
                filters_file=filters_path,
                conflict_resolve=pair["conflict_resolve"],
                resync=resync,
                conf_path=settings.data_dir / "rclone.conf",
            )
            ok = (result.returncode == 0)
            stats = {"added": 0, "deleted": 0, "changed": 0, "conflicts": 0}
            err_summary = result.stderr.strip()[-300:] if not ok and result.stderr else None
            repos.finalize_run(
                db, run_id=run_id,
                exit_code=result.returncode,
                status="ok" if ok else "fail",
                log_path=str(log_path),
                error_summary=err_summary,
                stats=stats,
            )
            repos.mark_pair_outcome(db, pair_id, success=ok)
            if ok and resync:
                repos.mark_pair_resynced(db, pair_id)
        except Exception as e:  # noqa: BLE001
            repos.finalize_run(
                db, run_id=run_id,
                exit_code=-1, status="fail",
                log_path=str(log_path),
                error_summary=f"orchestrator error: {type(e).__name__}: {e}",
            )
            repos.mark_pair_outcome(db, pair_id, success=False)


def attach_watchers(app) -> WatcherManager:
    """Construct a WatcherManager bound to this app's trigger_run.
    Called from main.py lifespan."""
    async def _trigger(pair_id: str, label: str, force_bisync: bool) -> None:
        # force_bisync currently unused — every trigger does a normal bisync.
        # The watcher's responsibility is deciding WHEN to call us; HOW (always
        # bisync) is unchanged.
        _ = force_bisync
        await trigger_run(app, pair_id, trigger=label, resync=False)
    return WatcherManager(app=app, trigger_fn=_trigger)


def restart_watchers(app) -> None:
    """Re-seed watchers from the DB. Called at startup and after pair edits."""
    db = app.state.db
    wm: WatcherManager = app.state.watchers
    wm.stop_all()
    for row in repos.list_pairs(db):
        if not row["paused"] and row["initial_resync_done"]:
            wm.start(row["id"], row["poll_seconds"])


### -------------- /pairs/new --------------


@router.get("/pairs/new")
async def pairs_new_form(request: Request) -> HTMLResponse:
    db = request.app.state.db
    remotes = repos.list_remotes(db)
    if not remotes:
        return HTMLResponse(
            '<h1>Add at least one remote first.</h1>'
            '<p><a href="/remotes">Connect Google or iCloud →</a></p>',
            status_code=400,
        )
    options = "".join(
        f'<option value="{r["name"]}">{r["name"]} ({r["provider"]} · {r["account_label"] or "—"})</option>'
        for r in remotes
    )
    page = (_STATIC_DIR / "pair_new.html").read_text()
    return HTMLResponse(page.replace("{{REMOTES}}", options))


@router.post("/pairs/new")
async def pairs_new_submit(request: Request,
                           name: str = Form(...),
                           source_remote: str = Form(...),
                           source_path: str = Form(""),
                           destination_remote: str = Form(...),
                           destination_path: str = Form(""),
                           filters: str = Form(""),
                           poll_seconds: int = Form(30)) -> RedirectResponse:
    db = request.app.state.db
    if source_remote == destination_remote and source_path == destination_path:
        raise HTTPException(400, "Source and destination must differ")
    pair_id = repos.create_pair(
        db,
        name=name.strip(),
        source_remote=source_remote,
        source_path=source_path.strip(),
        destination_remote=destination_remote,
        destination_path=destination_path.strip(),
        filters=filters,
        poll_seconds=max(10, min(3600, poll_seconds)),
    )
    # Watcher gets started after initial resync; nothing to do here yet.
    return RedirectResponse(f"/pairs/{pair_id}", status_code=302)


### -------------- /pairs/<id> detail --------------


@router.get("/pairs/{pair_id}")
async def pair_detail(pair_id: str, request: Request) -> HTMLResponse:
    db = request.app.state.db
    p = repos.get_pair(db, pair_id)
    if p is None:
        raise HTTPException(404, "pair not found")
    runs = repos.list_pair_runs(db, pair_id)
    runs_html = "".join(
        f'<tr><td>{r["started_at"]}</td><td>{r["status"]}</td>'
        f'<td>{r["trigger"]}</td><td>{r["exit_code"]}</td>'
        f'<td>added={r["files_added"] or 0} del={r["files_deleted"] or 0} '
        f'changed={r["files_changed"] or 0} conflicts={r["conflicts"] or 0}</td></tr>'
        for r in runs
    ) or '<tr><td colspan="5" class="muted">No runs yet.</td></tr>'
    page = (_STATIC_DIR / "pair_detail.html").read_text()
    initial_label = "needed" if not p["initial_resync_done"] else "done"
    return HTMLResponse(page
        .replace("{{NAME}}", p["name"])
        .replace("{{ID}}", pair_id)
        .replace("{{SOURCE}}", f'{p["source_remote"]}:{p["source_path"]}')
        .replace("{{DEST}}", f'{p["destination_remote"]}:{p["destination_path"]}')
        .replace("{{POLL}}", str(p["poll_seconds"]))
        .replace("{{FILTERS}}", p["filters"] or "(none)")
        .replace("{{INITIAL}}", initial_label)
        .replace("{{RUNS}}", runs_html))


### -------------- run / resync / pause / delete --------------


@router.post("/pairs/{pair_id}/run")
async def pair_run(pair_id: str, request: Request) -> RedirectResponse:
    return await _trigger(pair_id, request, trigger="manual", resync=False)


@router.post("/pairs/{pair_id}/resync")
async def pair_resync(pair_id: str, request: Request) -> RedirectResponse:
    return await _trigger(pair_id, request, trigger="resync", resync=True)


@router.post("/pairs/{pair_id}/pause")
async def pair_pause(pair_id: str, request: Request) -> RedirectResponse:
    db = request.app.state.db
    p = repos.get_pair(db, pair_id)
    if p is None:
        raise HTTPException(404)
    new_paused = not p["paused"]
    repos.set_pair_paused(db, pair_id, new_paused)
    if new_paused:
        request.app.state.watchers.stop(pair_id)
    elif p["initial_resync_done"]:
        request.app.state.watchers.start(pair_id, p["poll_seconds"])
    return RedirectResponse(f"/pairs/{pair_id}", status_code=302)


@router.post("/pairs/{pair_id}/delete")
async def pair_delete(pair_id: str, request: Request) -> RedirectResponse:
    db = request.app.state.db
    request.app.state.watchers.stop(pair_id)
    repos.delete_pair(db, pair_id)
    return RedirectResponse("/pairs", status_code=302)


async def _trigger(pair_id: str, request: Request, *, trigger: str, resync: bool) -> RedirectResponse:
    pair = repos.get_pair(request.app.state.db, pair_id)
    if pair is None:
        raise HTTPException(404)
    lock = _lock_for(pair_id)
    if lock.locked():
        return RedirectResponse(f"/pairs/{pair_id}?busy=1", status_code=302)

    async def _runner():
        await trigger_run(request.app, pair_id, trigger=trigger, resync=resync)
        # First successful resync flips initial_resync_done — start the watcher
        if resync:
            row = repos.get_pair(request.app.state.db, pair_id)
            if row and row["initial_resync_done"] and not row["paused"]:
                request.app.state.watchers.start(pair_id, row["poll_seconds"])

    asyncio.create_task(_runner())
    return RedirectResponse(f"/pairs/{pair_id}?queued=1", status_code=302)
