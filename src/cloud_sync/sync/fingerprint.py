"""Cheap fingerprint of an rclone remote tree.

Used to detect "did anything change on the iCloud side?" without doing a
full bisync. We run `rclone lsf -R --format=tsp` and sha256 the output:

  t = mtime
  s = size
  p = path

Caveats for iclouddrive specifically:
  - rclone reports modtime "2000-01-01 00:00:00" for iCloud entries where
    Apple didn't expose a real mtime. That's noisy but stable per-file —
    the fingerprint stays the same as long as size+path don't change.
  - True content edits without size change (rare for markdown) won't
    register here. Bisync still runs at the idle-floor cadence, so they
    propagate eventually.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


async def compute(remote_path: str, *, conf_path: Path) -> str | None:
    """Run `rclone lsf -R --format=tsp <remote:path>` and return the sha256
    hex of its stdout. None on failure."""
    binary = shutil.which("rclone")
    if binary is None:
        return None
    proc = await asyncio.create_subprocess_exec(
        binary, "--config", str(conf_path), "lsf", "-R", "--format=tsp",
        remote_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("fingerprint failed for %s: rc=%d stderr=%s",
                       remote_path, proc.returncode, err.decode()[:200])
        return None
    # iCloud (and other providers) return listing entries in non-deterministic
    # order. Sort line-by-line so the hash is order-independent.
    sorted_bytes = b"\n".join(sorted(out.splitlines()))
    return hashlib.sha256(sorted_bytes).hexdigest()
