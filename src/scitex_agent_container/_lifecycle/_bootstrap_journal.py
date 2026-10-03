"""Durable, exclusive receipts for one same-process bootstrap completion."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID


class BootstrapJournal:
    """A consumed attempt remains on disk after success, refusal, or a crash."""

    def __init__(self, state: Path, launch_id: str) -> None:
        if str(UUID(launch_id)) != launch_id or not state.is_absolute():
            raise ValueError(
                "bootstrap journal requires an absolute state and launch ID"
            )
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in state.parts[1:]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.close(parent)
                parent = child
            if os.fstat(parent).st_uid != os.getuid():
                raise RuntimeError("bootstrap state directory has a foreign owner")
            name = f"native-bootstrap-completion-{launch_id}"
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
            self.fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            self.path = state / name
        finally:
            os.close(parent)

    def write(self, name: str, value: dict) -> None:
        """Write a new receipt completely, then sync its containing directory."""
        if name not in {"claim", "bridge", "inbox", "cct", "registration", "result"}:
            raise ValueError("unknown bootstrap receipt phase")
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        fd = os.open(
            name + ".json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("bootstrap receipt write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)
