"""Build /data/rclone.conf from the rclone_remotes table.

For Google: each remote has its SA JSON key written to
/data/credentials/<name>.json (mode 0600); the rclone stanza references
that file via `service_account_file=`.

For iCloud: the encrypted blob in the DB has the apple-id + obscured
password; the rclone stanza is built directly from those.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from cloud_sync.providers import dropbox as dropbox_provider
from cloud_sync.providers import google as google_provider
from cloud_sync.providers import icloud as icloud_provider
from cloud_sync.sync import encryption, rclone

logger = logging.getLogger(__name__)


async def rebuild(db: sqlite3.Connection, *, conf_path: Path,
                  age_secret_key: str, credentials_dir: Path) -> None:
    """Read every row in rclone_remotes, decrypt, materialize per-remote
    artifacts (SA key files), assemble the full rclone.conf."""
    credentials_dir.mkdir(parents=True, exist_ok=True)
    stanzas: dict[str, dict[str, str]] = {}
    keep_keys: set[str] = set()

    rows = list(db.execute(
        "select name, provider, encrypted_config from rclone_remotes"
    ))
    for row in rows:
        name = row["name"]
        provider = row["provider"]
        try:
            blob = await encryption.decrypt(row["encrypted_config"], age_secret_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not decrypt remote %r: %s — skipping", name, e)
            continue

        if provider == "google":
            sa = google_provider.deserialize(blob)
            key_path = credentials_dir / f"{name}.json"
            key_path.write_text(sa.raw_json)
            key_path.chmod(0o600)
            keep_keys.add(key_path.name)
            stanzas[name] = rclone.google_drive_stanza(
                service_account_file=str(key_path),
            )
        elif provider == "icloud":
            c = icloud_provider.deserialize(blob)
            stanzas[name] = rclone.icloud_stanza(
                apple_id=c.apple_id,
                password_obscured=c.password_obscured,
                trust_token=c.trust_token,
                cookies=c.cookies,
            )
        elif provider == "dropbox":
            c = dropbox_provider.deserialize(blob)
            stanzas[name] = rclone.dropbox_stanza(
                client_id=c.client_id,
                client_secret=c.client_secret,
                access_token=c.access_token,
                refresh_token=c.refresh_token,
                expiry=c.expiry_iso,
            )
        else:
            logger.warning("provider %r not yet supported", provider)

    # Garbage-collect orphaned credential files
    for f in credentials_dir.glob("*.json"):
        if f.name not in keep_keys:
            try:
                f.unlink()
                logger.info("removed orphaned credential file %s", f)
            except OSError:
                pass

    rclone.write_rclone_conf(conf_path, stanzas)
    logger.info("wrote %s with %d stanzas", conf_path, len(stanzas))
