"""SQLite open + migration runner.

Migrations are plain .sql files in the top-level migrations/ dir, applied
once each in filename order. State is tracked in a tiny `schema_migrations`
table.

Note on encryption (Phase 4 — Syncfox): we encrypt credential BLOBS at
the application layer (Fernet, in `sync/encryption.py`) rather than
encrypting the whole SQLite file. Reason: SQLCipher needs a per-arch
binary (no arm64 wheels), which would complicate the multi-arch Docker
image. Application-level encryption uses the `cryptography` package's
universal wheels and protects the same threat model — anyone reading
`/data/cloudsync.db` off disk gets unintelligible blobs in the
`encrypted_config` column.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def open_db(path: Path) -> sqlite3.Connection:
    """Open the SQLite DB and apply any pending migrations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "create table if not exists schema_migrations (filename text primary key, applied_at text default (datetime('now')))"
    )
    applied = {row["filename"] for row in conn.execute("select filename from schema_migrations")}
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name in applied:
            continue
        sql = migration.read_text()
        logger.info("applying migration %s", migration.name)
        with conn:
            conn.executescript(sql)
            conn.execute("insert into schema_migrations(filename) values (?)", (migration.name,))
