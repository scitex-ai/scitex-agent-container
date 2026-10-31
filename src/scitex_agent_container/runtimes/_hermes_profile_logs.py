"""Provision Hermes-generated logs without following imported symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

_GENERATED_LOG_NAMES = ("agent.log", "gui.log")


def _archive_link(logs_fd: int, name: str) -> str:
    """Reserve an archive name, then move the link itself into it."""
    while True:
        archive = f"{name}.sac-link-{uuid4().hex}"
        try:
            reservation = os.open(
                archive,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=logs_fd,
            )
        except FileExistsError:
            continue
        os.close(reservation)
        try:
            os.rename(name, archive, src_dir_fd=logs_fd, dst_dir_fd=logs_fd)
        except OSError:
            os.unlink(archive, dir_fd=logs_fd)
            raise
        return archive


def ensure_hermes_log_files(profile: Path) -> tuple[Path, ...]:
    """Prepare the two generated append logs in an exclusively held profile.

    Imported profiles can contain absolute links to another host's log
    directory. Preserve those link objects locally and create regular files
    for Hermes instead. Existing regular logs, rotations, and link targets
    are untouched. Call before launch, or from a stopped/erroring runtime's
    container namespace; never modify a live mounted overlay from the host.
    """
    logs = profile / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    logs_fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    archives: list[Path] = []
    try:
        # Inspect both entries before changing either: unexpected data must
        # fail closed rather than leave a partially provisioned profile.
        for name in _GENERATED_LOG_NAMES:
            try:
                entry = os.stat(name, dir_fd=logs_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
                raise ValueError(
                    f"Hermes generated log is not a file or link: {logs / name}"
                )
        for name in _GENERATED_LOG_NAMES:
            try:
                entry = os.stat(name, dir_fd=logs_fd, follow_symlinks=False)
            except FileNotFoundError:
                entry = None
            if entry is not None and stat.S_ISLNK(entry.st_mode):
                archives.append(logs / _archive_link(logs_fd, name))
            fd = os.open(
                name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=logs_fd,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError(
                        f"Hermes generated log is not a regular file: {logs / name}"
                    )
            finally:
                os.close(fd)
    finally:
        os.close(logs_fd)
    return tuple(archives)


__all__ = ["ensure_hermes_log_files"]
