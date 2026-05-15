"""rclone subprocess wrapper.

We never leave rclone configuration to its built-in `rclone config` flow —
that wants a TTY. Instead we write `rclone.conf` ourselves into /data and
pass `--config` on every invocation. Each remote is a stanza with a unique
name; the on-disk `.conf` is regenerated from `rclone_remotes` rows on
container start and on every remote create / update / delete.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from configparser import ConfigParser
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


def binary_path() -> str | None:
    return shutil.which("rclone")


async def version() -> str:
    binary = binary_path()
    if binary is None:
        raise RuntimeError("rclone binary not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        binary, "version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"rclone version exited {proc.returncode}: {out!r}")
    return out.decode("utf-8", errors="replace").splitlines()[0].strip()


### -------------- rclone.conf builder --------------


def write_rclone_conf(conf_path: Path, stanzas: dict[str, dict[str, str]]) -> None:
    """Write `rclone.conf` from per-remote stanzas.

    `stanzas` is `{remote_name: {key: value, ...}}`. Each remote becomes a
    `[remote_name]` section. Keys are rclone backend params (e.g.
    `type=drive`, `client_id=…`, `apple_id=…`).
    """
    cfg = ConfigParser()
    for name, kv in stanzas.items():
        cfg[name] = kv
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    buf = StringIO()
    cfg.write(buf, space_around_delimiters=False)
    conf_path.write_text(buf.getvalue())
    conf_path.chmod(0o600)


def google_drive_stanza(*, service_account_file: str,
                        team_drive: str = "") -> dict[str, str]:
    """rclone google drive backend stanza for service-account auth.

    `service_account_file` is the absolute path to the SA JSON key file
    inside the container — typically /data/credentials/<remote>.json
    (mode 0600, written by the provider's add flow). `team_drive` is the
    shared-drive ID if this remote points at a Workspace shared drive;
    leave blank for the SA's own / per-folder shared access.

    `shared_with_me=true` is set unconditionally: an SA has no "My Drive"
    content of its own, so the only useful root is the folders explicitly
    shared with the SA's email. Without this flag, listing the SA root
    returns nothing — even though the SA can see those folders.
    """
    return {
        "type": "drive",
        "scope": "drive",
        "service_account_file": service_account_file,
        "team_drive": team_drive,
        "shared_with_me": "true",
    }


def icloud_stanza(*, apple_id: str, password_obscured: str,
                  trust_token: str, cookies: str) -> dict[str, str]:
    """rclone iclouddrive backend stanza values.

    `password_obscured` must be pre-obscured via `rclone obscure`.
    `trust_token` and `cookies` come from a successful 2FA dance — see
    providers/icloud and routes/remotes_icloud_2fa for the flow.
    """
    return {
        "type": "iclouddrive",
        "apple_id": apple_id,
        "password": password_obscured,
        "trust_token": trust_token,
        "cookies": cookies,
    }


def dropbox_stanza(*, client_id: str, client_secret: str, refresh_token: str,
                   access_token: str = "", expiry: str = "") -> dict[str, str]:
    token_json = (
        '{'
        f'"access_token":"{access_token}",'
        f'"refresh_token":"{refresh_token}",'
        '"token_type":"bearer",'
        f'"expiry":"{expiry or "0001-01-01T00:00:00Z"}"'
        '}'
    )
    return {
        "type": "dropbox",
        "client_id": client_id,
        "client_secret": client_secret,
        "token": token_json,
    }


### -------------- rclone subprocess primitives --------------


@dataclass(frozen=True, slots=True)
class RcloneResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float


async def _run(*args: str, conf_path: Path) -> RcloneResult:
    binary = binary_path()
    if binary is None:
        raise RuntimeError("rclone binary not found")
    cmd = [binary, "--config", str(conf_path), *args]
    logger.debug("rclone exec: %s", " ".join(cmd))
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return RcloneResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        elapsed_ms=(time.perf_counter() - t0) * 1000,
    )


async def obscure(plaintext: str) -> str:
    """rclone-obscure a plaintext password (used by the iCloud flow)."""
    binary = binary_path()
    if binary is None:
        raise RuntimeError("rclone binary not found")
    proc = await asyncio.create_subprocess_exec(
        binary, "obscure", plaintext,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"rclone obscure failed: {err.decode()!r}")
    return out.decode().strip()


async def lsd(remote: str, path: str = "", *, conf_path: Path) -> RcloneResult:
    """List directories — used to test-connect a remote."""
    target = f"{remote}:{path}" if path else f"{remote}:"
    return await _run("lsd", target, "--max-depth", "1", conf_path=conf_path)


async def bisync(*, source: str, destination: str, work_dir: Path, log_path: Path,
                 filters_file: Path | None, conflict_resolve: str = "newer",
                 resync: bool = False, conf_path: Path) -> RcloneResult:
    """Run a bisync between two rclone remotes (or remote:path pairs).

    `--check-access` requires a `RCLONE_TEST` sentinel file in each remote root
    to ensure neither side went dark. The first-ever run for a pair must pass
    `resync=True` (calls `rclone bisync --resync`) to establish baseline state.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "bisync", source, destination,
        "--workdir", str(work_dir),
        "--conflict-resolve", conflict_resolve,
        "--conflict-suffix", "COPY-{DateOnly}",
        "--resilient", "--recover",
        "--log-file", str(log_path),
        "--log-level", "INFO",
    ]
    if filters_file is not None:
        args.extend(["--filter-from", str(filters_file)])
    if resync:
        args.append("--resync")
    return await _run(*args, conf_path=conf_path)
