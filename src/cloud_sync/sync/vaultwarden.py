"""Optional Vaultwarden / Bitwarden secrets backend.

If `VAULTWARDEN_URL` + `VAULTWARDEN_ITEM_ID` + `VAULTWARDEN_EMAIL` +
`VAULTWARDEN_MASTER_PASSWORD` are set, this module fetches selected
secrets from a Bitwarden item at startup (subprocess-out to the `bw`
CLI) and merges them into the Settings instance — overriding plain
`.env` values.

Use case: operator wants their secrets in one place across multiple
self-hosted services. Optional. No-op if any of the four vars is unset.

Phase 2 (Syncfox) — minimal implementation. Extends naturally as more
secrets need to come from the vault.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def maybe_fetch(settings: Any) -> None:
    """If Vaultwarden env vars are all set, replace settings fields with
    values from the named vault item's `fields` array. Mutates settings
    in place. No-op otherwise."""
    url = os.environ.get("VAULTWARDEN_URL", "")
    item_id = os.environ.get("VAULTWARDEN_ITEM_ID", "")
    email = os.environ.get("VAULTWARDEN_EMAIL", "")
    master_pw = os.environ.get("VAULTWARDEN_MASTER_PASSWORD", "")
    if not (url and item_id and email and master_pw):
        return  # operator opted out

    bw = _which_bw()
    if bw is None:
        logger.warning("VAULTWARDEN_* env set but `bw` CLI not found in PATH — skipping vault load")
        return

    try:
        # Configure server (idempotent)
        subprocess.run([bw, "config", "server", url], capture_output=True, check=True, timeout=10)
        # Login to get a session
        login = subprocess.run([bw, "login", email, master_pw, "--raw"],
                               capture_output=True, text=True, timeout=20)
        if login.returncode != 0 and "already" not in (login.stderr or "").lower():
            logger.warning("vaultwarden login failed: %s", login.stderr.strip()[:200])
            return
        # If already logged in, unlock instead
        if login.returncode != 0:
            unlock = subprocess.run([bw, "unlock", master_pw, "--raw"],
                                    capture_output=True, text=True, timeout=20)
            session = unlock.stdout.strip()
            if not session:
                logger.warning("vaultwarden unlock failed: %s", unlock.stderr.strip()[:200])
                return
        else:
            session = login.stdout.strip()

        # Fetch the item
        env = {**os.environ, "BW_SESSION": session}
        out = subprocess.run([bw, "get", "item", item_id, "--session", session],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            logger.warning("vaultwarden get item failed: %s", out.stderr.strip()[:200])
            return
        item = json.loads(out.stdout)
        fields = {f["name"]: f.get("value", "") for f in item.get("fields", [])}

        # Map known field names → settings attributes. Operator names the
        # field exactly the env var (case-insensitive).
        mapping = {
            "GOOGLE_OAUTH_CLIENT_ID": "google_oauth_client_id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "google_oauth_client_secret",
            "DROPBOX_OAUTH_CLIENT_ID": "dropbox_oauth_client_id",
            "DROPBOX_OAUTH_CLIENT_SECRET": "dropbox_oauth_client_secret",
            "AGE_SECRET_KEY": "age_secret_key",
            "SYNCFOX_DISCORD_WEBHOOK_URL": "syncfox_discord_webhook_url",
        }
        applied = []
        for env_name, attr in mapping.items():
            value = fields.get(env_name) or fields.get(env_name.lower())
            if value:
                object.__setattr__(settings, attr, value)
                applied.append(env_name)
        logger.info("vaultwarden: loaded %d secret(s) from item %s — %s",
                    len(applied), item_id[:8], ", ".join(applied) or "(none matched)")

        # Lock the vault on the way out
        subprocess.run([bw, "lock"], capture_output=True, timeout=5)
    except Exception as e:  # noqa: BLE001 — secrets backend should never crash boot
        logger.warning("vaultwarden integration failed (boot continues with .env values): %s", e)


def _which_bw() -> str | None:
    """Find the bw CLI binary. Common locations: /usr/local/bin, ~/bin."""
    for candidate in ("bw", "/usr/local/bin/bw", "/usr/bin/bw", os.path.expanduser("~/bin/bw")):
        try:
            r = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None
