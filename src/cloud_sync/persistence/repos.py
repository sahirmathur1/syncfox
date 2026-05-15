"""Query helpers — keep SQL out of route handlers."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Iterable


### -------------- rclone_remotes --------------


def list_remotes(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute(
        "select name, provider, account_label, created_at, last_verified_at "
        "from rclone_remotes order by created_at desc"
    ))


def get_remote(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return db.execute(
        "select * from rclone_remotes where name = ?", (name,),
    ).fetchone()


def upsert_remote(db: sqlite3.Connection, *, name: str, provider: str,
                  account_label: str | None, encrypted_config: str) -> None:
    with db:
        db.execute(
            """insert into rclone_remotes (name, provider, account_label, encrypted_config, last_verified_at)
               values (?, ?, ?, ?, datetime('now'))
               on conflict(name) do update set
                 provider=excluded.provider,
                 account_label=excluded.account_label,
                 encrypted_config=excluded.encrypted_config,
                 last_verified_at=excluded.last_verified_at""",
            (name, provider, account_label, encrypted_config),
        )


def delete_remote(db: sqlite3.Connection, name: str) -> None:
    with db:
        db.execute("delete from rclone_remotes where name = ?", (name,))


### -------------- pairs --------------


def list_pairs(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute(
        "select * from pairs order by created_at desc"
    ))


def get_pair(db: sqlite3.Connection, pair_id: str) -> sqlite3.Row | None:
    return db.execute("select * from pairs where id = ?", (pair_id,)).fetchone()


def create_pair(db: sqlite3.Connection, *, name: str,
                source_remote: str, source_path: str,
                destination_remote: str, destination_path: str,
                poll_seconds: int = 30, filters: str = "",
                conflict_resolve: str = "newer") -> str:
    pair_id = str(uuid.uuid4())
    with db:
        db.execute(
            """insert into pairs (id, name, source_remote, source_path,
                                  destination_remote, destination_path,
                                  poll_seconds, filters, conflict_resolve)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pair_id, name, source_remote, source_path,
             destination_remote, destination_path,
             poll_seconds, filters, conflict_resolve),
        )
    return pair_id


def set_pair_paused(db: sqlite3.Connection, pair_id: str, paused: bool) -> None:
    with db:
        db.execute("update pairs set paused = ? where id = ?", (1 if paused else 0, pair_id))


def mark_pair_resynced(db: sqlite3.Connection, pair_id: str) -> None:
    with db:
        db.execute("update pairs set initial_resync_done = 1 where id = ?", (pair_id,))


def mark_pair_outcome(db: sqlite3.Connection, pair_id: str, *, success: bool) -> None:
    col = "last_success_at" if success else "last_failure_at"
    with db:
        db.execute(f"update pairs set {col} = datetime('now') where id = ?", (pair_id,))


def set_drive_watermark(db: sqlite3.Connection, pair_id: str, token: str) -> None:
    with db:
        db.execute("update pairs set drive_changes_token = ? where id = ?",
                   (token, pair_id))


def mark_full_bisync_done(db: sqlite3.Connection, pair_id: str) -> None:
    with db:
        db.execute("update pairs set last_full_bisync_at = datetime('now') where id = ?",
                   (pair_id,))


def set_icloud_fingerprint(db: sqlite3.Connection, pair_id: str, fp: str) -> None:
    with db:
        db.execute("update pairs set icloud_fingerprint = ? where id = ?",
                   (fp, pair_id))


def delete_pair(db: sqlite3.Connection, pair_id: str) -> None:
    with db:
        db.execute("delete from pairs where id = ?", (pair_id,))


### -------------- pair_runs --------------


def insert_run(db: sqlite3.Connection, *, pair_id: str, trigger: str) -> str:
    run_id = str(uuid.uuid4())
    with db:
        db.execute(
            "insert into pair_runs (id, pair_id, started_at, trigger, status) "
            "values (?, ?, datetime('now'), ?, 'running')",
            (run_id, pair_id, trigger),
        )
    return run_id


def finalize_run(db: sqlite3.Connection, *, run_id: str, exit_code: int,
                 status: str, log_path: str | None,
                 error_summary: str | None = None,
                 stats: dict[str, int] | None = None) -> None:
    s = stats or {}
    with db:
        db.execute(
            """update pair_runs set
                 ended_at      = datetime('now'),
                 exit_code     = ?,
                 status        = ?,
                 log_path      = ?,
                 error_summary = ?,
                 files_added   = ?,
                 files_deleted = ?,
                 files_changed = ?,
                 conflicts     = ?
               where id = ?""",
            (exit_code, status, log_path, error_summary,
             s.get("added"), s.get("deleted"), s.get("changed"), s.get("conflicts"),
             run_id),
        )


def list_pair_runs(db: sqlite3.Connection, pair_id: str, limit: int = 10) -> list[sqlite3.Row]:
    return list(db.execute(
        "select * from pair_runs where pair_id = ? order by started_at desc limit ?",
        (pair_id, limit),
    ))
