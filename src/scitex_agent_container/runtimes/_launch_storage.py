"""Resolved launch-storage evidence and byte-based admission guard."""

from __future__ import annotations

import scitex_logging as slogging
import os
from pathlib import Path
from typing import Callable

logger = slogging.getLogger(__name__)

WARN_FREE_BYTES = 20 * 1024**3
REFUSE_ROOT_FREE_BYTES = 5 * 1024**3


class LaunchStorageSpaceError(RuntimeError):
    """A write-heavy launch target is on a critically full root filesystem."""


def _flag_paths(argv: list[str]) -> dict[str, str]:
    """Return the exact storage paths selected by the finished argv."""
    found: dict[str, str] = {}
    for i, arg in enumerate(argv):
        if arg == "--overlay":
            if i + 1 < len(argv):
                found.setdefault("overlay", argv[i + 1])
        elif arg.startswith("--overlay="):
            found.setdefault("overlay", arg.split("=", 1)[1])
        elif arg in ("-W", "--workdir"):
            if i + 1 < len(argv):
                found.setdefault("apptainer_workdir", argv[i + 1])
        elif arg.startswith("--workdir="):
            found.setdefault("apptainer_workdir", arg.split("=", 1)[1])
    return found


def launch_storage_identity(argv: list[str]) -> dict[str, str]:
    """Birth-certificate-safe exact paths from the argv that actually runs."""
    return {
        key: str(Path(value).expanduser().absolute())
        for key, value in _flag_paths(argv).items()
    }


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _free_bytes(path: Path, statvfs_fn: Callable = os.statvfs) -> int:
    stats = statvfs_fn(path)
    return stats.f_bavail * stats.f_frsize


def assess_launch_path(
    *, label: str, path: str, backing: Path, free: int, root_backed: bool
) -> None:
    """Apply the threshold policy to already-observed filesystem facts."""
    if free < WARN_FREE_BYTES:
        logger.warning(
            "launch storage warning: %s=%s is backed by %s with %d bytes free "
            "(< %d); use this host's scratch_root for write-heavy agent state",
            label,
            path,
            backing,
            free,
            WARN_FREE_BYTES,
        )
    if root_backed and free < REFUSE_ROOT_FREE_BYTES:
        raise LaunchStorageSpaceError(
            f"refusing root-backed write-heavy launch: {label}={path} "
            f"is backed by {backing}, which has {free} bytes free "
            f"(< {REFUSE_ROOT_FREE_BYTES}). Point spec.apptainer.overlay "
            "and the managed Apptainer workdir at the host scratch_root; "
            "no process was started."
        )


def verify_launch_storage(
    argv: list[str],
    *,
    stat_fn: Callable = os.stat,
    statvfs_fn: Callable = os.statvfs,
) -> None:
    """Warn below 20 GiB; refuse root-backed write-heavy paths below 5 GiB.

    The finished argv is the authority: explicit spec ``--overlay`` and
    ``--workdir`` values therefore win automatically.  This function is only
    called after dry-run handling, immediately before launch.
    """
    root_device = stat_fn("/").st_dev
    checked: set[tuple[int, str]] = set()
    for label, raw_path in _flag_paths(argv).items():
        backing = _existing_ancestor(Path(raw_path))
        device = stat_fn(backing).st_dev
        key = (device, str(backing))
        if key in checked:
            continue
        checked.add(key)
        free = _free_bytes(backing, statvfs_fn)
        assess_launch_path(
            label=label,
            path=raw_path,
            backing=backing,
            free=free,
            root_backed=device == root_device,
        )


__all__ = [
    "LaunchStorageSpaceError",
    "REFUSE_ROOT_FREE_BYTES",
    "WARN_FREE_BYTES",
    "assess_launch_path",
    "launch_storage_identity",
    "verify_launch_storage",
]
