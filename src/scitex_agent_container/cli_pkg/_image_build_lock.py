"""Cross-process exclusion for one SAC image layer build."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, TextIO


class ImageBuildInProgress(RuntimeError):
    """Raised when another process owns a layer's mutable build context."""


def _owner_text(handle: TextIO) -> str:
    handle.seek(0)
    raw = handle.read().strip()
    if not raw:
        return "owner details unavailable"
    try:
        owner = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return (
        ", ".join(
            f"{key}={owner[key]}"
            for key in ("pid", "host", "started_at")
            if key in owner
        )
        or "owner details unavailable"
    )


@contextmanager
def _lock_file(lock_path: Path) -> Iterator[TextIO]:
    """Admit a single-link regular inode without following the final symlink."""
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(lock_path, flags, 0o666)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(
                errno.EINVAL,
                "refusing build lock that is not a singly-linked regular file",
                str(lock_path),
            )
        with os.fdopen(descriptor, "a+", encoding="utf-8", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


@contextmanager
def image_build_lock(artifact_dir: Path, *, layer: str) -> Iterator[None]:
    """Own one layer's staging directory exclusively or fail immediately.

    The final lock entry must be a singly-linked regular file. Ancestor
    directories are outside this entry-admission check.

    The kernel releases ``flock`` when a process exits, including after an
    interrupted build.  The lock spans staging and the complete container
    build because Apptainer continues reading the staged ``%files`` inputs.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    lock_path = artifact_dir / ".build.lock"
    with _lock_file(lock_path) as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = _owner_text(handle)
            raise ImageBuildInProgress(
                f"refusing concurrent sac image build for layer '{layer}': "
                f"{lock_path} is held ({owner}). Wait for that build to finish "
                "or stop it before retrying."
            ) from exc

        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        json.dump(owner, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["ImageBuildInProgress", "image_build_lock"]
