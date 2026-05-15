"""Unit test: migration runner is idempotent and creates expected tables."""
import tempfile
from pathlib import Path

from cloud_sync.persistence.db import open_db


def test_open_db_applies_migrations_once_and_creates_expected_tables():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        # First open — applies migrations
        conn = open_db(path)
        tables = {row[0] for row in conn.execute(
            "select name from sqlite_master where type='table' order by name"
        )}
        conn.close()
        # Re-open — should not re-apply
        conn = open_db(path)
        applied = conn.execute("select count(*) from schema_migrations").fetchone()[0]
        conn.close()

    assert "rclone_remotes" in tables
    assert "pairs" in tables
    assert "pair_runs" in tables
    assert "schema_migrations" in tables
    assert applied == 1  # only 001_init.sql so far
