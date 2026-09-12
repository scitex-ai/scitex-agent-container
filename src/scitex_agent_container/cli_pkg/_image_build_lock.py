"""Cross-process exclusion for one SAC image layer build."""

from __future__ import annotations

import fcntl
import json
import os
import socket
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
def image_build_lock(artifact_dir: Path, *, layer: str) -> Iterator[None]:
    """Own one layer's staging directory exclusively or fail immediately.

    The kernel releases ``flock`` when a process exits, including after an
    interrupted build.  The lock spans staging and the complete container
    build because Apptainer continues reading the staged ``%files`` inputs.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    lock_path = artifact_dir / ".build.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
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
