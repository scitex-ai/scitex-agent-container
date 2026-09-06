"""Single-instance guard for ``sac image bake-remote``.

MEASURED INCIDENT, 2026-09-06/07 on scitex-compute-03. Nothing stopped a
second ``bake-remote`` starting while one was mid-transfer, so a
supervisor restart began a SECOND ~7.6G rsync of the SAME artifact. Both
used ``rsync --partial``, so the newcomer resumed from the incumbent's
partial AND wrote its own temp — consuming more than it replaced. The
host went 17G free -> 3.8G in fifteen minutes with THREE concurrent
pulls of one artifact, and earlier the same evening the same loop had
driven the disk to zero and the ecosystem supervisor to 273 restarts.

Killing a duplicate does not help: the supervisor re-runs the job when
it exits, so each kill starts a fresh full pull. The only thing that
ends it is refusing the second START.

WHY A LOCK RATHER THAN A SURVEY. Asking "is another bake running?" and
then starting is two steps with a gap, and the gap is exactly where the
second one gets in — measured here as a third bake appearing 55 seconds
after the second was killed. ``flock(LOCK_EX | LOCK_NB)`` decides
atomically: acquired means nobody else holds it AND nobody else can
take it while we do.

Mechanism follows ``_listen/_single_instance`` (flock-backed pidfile,
holder PID in the body for diagnostics, kernel releases on any exit
including SIGKILL, so a crashed bake never jams the pipeline). That
module is correctly scoped to listen — it is parameterised on a TCP
port — so this is a sibling rather than a caller. Unifying the two onto
one primitive is worth doing and is deliberately NOT done here: the
listen lock is on the daemon's critical path and this landed while a
supervisor was stopped.

SCOPED PER CONTAINERS DIR, not per host and not global. Two bakes into
DIFFERENT containers dirs cannot collide (they write different files),
and a global lock would make an unrelated dev bake block the timer.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BakeLockHandle",
    "BakeAlreadyRunningError",
    "acquire_bake_lock",
    "release_bake_lock",
]


@dataclass
class BakeLockHandle:
    """Opaque handle to an acquired bake lock.

    ``fd`` holds the flock; closing it releases the lock. Keep the
    handle alive for the whole bake.
    """

    fd: int
    pid_file: Path


class BakeAlreadyRunningError(RuntimeError):
    """Another ``bake-remote`` holds the lock for this containers dir.

    Carries the holding PID and the lock path so the operator can name
    and clear the conflict without ``lsof``.
    """


def bake_lock_path(containers_dir: Path, lock_dir: Path) -> Path:
    """Lock path for ``containers_dir``, hashed so it is filesystem-safe.

    The absolute containers dir is the identity — a short digest of it
    names the file, because the path itself contains separators and can
    be long. Deterministic, so two invocations naming the same dir by
    the same absolute path collide as intended.
    """
    digest = hashlib.sha256(str(containers_dir.resolve()).encode()).hexdigest()[:16]
    return lock_dir / f"bake-remote-{digest}.pid"


def _read_holding_pid(fd: int) -> str:
    """Best-effort PID read for the diagnostic message only.

    Never branched on — the flock is the source of truth.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 256).decode("utf-8", "replace").strip() or "<unknown>"
    except OSError:  # stx-allow: fallback (reason: diagnostic-only — flock decision already made)
        return "<unreadable>"


def acquire_bake_lock(
    *,
    containers_dir: Path,
    lock_dir: Path,
) -> BakeLockHandle:
    """Take the exclusive bake lock for ``containers_dir``.

    ``lock_dir`` must exist; the caller owns creating it, so tests can
    isolate without touching ``$HOME``.

    Raises :class:`BakeAlreadyRunningError` when another bake holds it.
    Refusing is correct: a second concurrent pull of a multi-GB artifact
    cannot help and demonstrably fills the disk.
    """
    pid_file = bake_lock_path(containers_dir, lock_dir)
    fd = os.open(str(pid_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        holding_pid = _read_holding_pid(fd)
        os.close(fd)
        raise BakeAlreadyRunningError(
            f"another `sac image bake-remote` is already pulling into "
            f"{containers_dir} (holding PID {holding_pid}, lock file "
            f"{pid_file}). A second concurrent pull of the same multi-GB "
            f"artifact cannot finish sooner and fills the disk — this one "
            f"is declining, NOT failing. Wait for that bake, or stop it "
            f"(`kill {holding_pid}`) if it is wedged."
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    return BakeLockHandle(fd=fd, pid_file=pid_file)


def release_bake_lock(handle: BakeLockHandle) -> None:
    """Release on clean exit. The kernel handles dirty exit."""
    try:
        os.close(handle.fd)
    except OSError:  # stx-allow: fallback (reason: fd already closed; the flock is gone either way)
        pass
