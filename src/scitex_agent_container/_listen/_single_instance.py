"""Single-instance guard for ``sac listen`` (operator task #26 sub (1)).

Background: before this guard, starting a second ``sac listen`` while
one already held the bound port resulted in uvicorn's
``OSError: [Errno 98] Address already in use`` and a hard-crash with a
Python traceback. The crash was loud (good) but the operator had no
diagnostic about which process held the port — they got a traceback,
not a clean message naming the holding PID.

This module replaces that experience with a flock-backed pidfile
guard. Three properties combined:

1. **flock-atomic**: ``fcntl.flock(LOCK_EX | LOCK_NB)`` is the source
   of truth for "is another listen running". The kernel releases the
   flock on process exit (even after SIGKILL / OOM), so a crashed
   listen never permanently jams the port. No stale-lock
   reconciliation needed.

2. **PID written for diagnostics**: the pidfile body carries the
   current PID so the conflict error message can name the holding
   process — ``kill <pid>`` is actionable without ``lsof`` or
   ``netstat``.

3. **Port-scoped**: the pidfile path is ``<lock_dir>/listen-<port>.pid``
   so two listens on different ports (e.g. a dev instance on 7879)
   don't fight each other.

NEVER bypassed by a stale pidfile: if a prior listen crashed leaving
its PID in the file, the flock is already released; the next
acquire finds an unlocked file, takes the flock, overwrites the PID.
This is the "stale pidfile + no live holder" path covered by
:func:`tests/.../_listen/test__single_instance.py::
test_stale_pidfile_with_no_flock_holder_can_be_reacquired`.

The caller MUST keep ``LockHandle.fd`` open for the lifetime of the
process — closing the fd releases the flock. Use :func:`release_listen_lock`
on clean exit; the kernel handles dirty exit.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LockHandle",
    "ListenAlreadyRunningError",
    "acquire_listen_lock",
    "release_listen_lock",
]


@dataclass
class LockHandle:
    """Opaque handle to an acquired listen lock.

    ``fd`` is the OS file descriptor holding the flock; closing it
    releases the lock. ``pid_file`` is the on-disk pidfile path, kept
    so :func:`release_listen_lock` can clean up.
    """

    fd: int
    pid_file: Path


class ListenAlreadyRunningError(RuntimeError):
    """Raised when another ``sac listen`` already holds the port lock.

    Carries the holding PID (read from the pidfile body) and the lock
    file path so the operator can both name and clear the conflict
    without external tooling.
    """


def _pid_file_path(port: int, lock_dir: Path) -> Path:
    """Return the pidfile path for ``port`` under ``lock_dir``.

    Port-scoped so two listens on different ports do not fight. No
    side-effects — the caller is responsible for ``mkdir -p`` on
    ``lock_dir`` (or relying on the default-path helper).
    """
    return lock_dir / f"listen-{port}.pid"


def _read_holding_pid(fd: int) -> str:
    """Best-effort read of the PID from the pidfile content.

    Returns an empty-ish placeholder (``"<unreadable>"``) on any read
    error — this is purely for the diagnostic message, never branched
    on. The flock itself is the source of truth.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 256).decode("utf-8", "replace").strip() or "<unknown>"
    except OSError:  # stx-allow: fallback (reason: diagnostic-only — file may be empty/unreadable; flock decision already made)
        return "<unreadable>"


def acquire_listen_lock(
    *,
    port: int,
    lock_dir: Path,
) -> LockHandle:
    """Acquire an exclusive flock for the listen-on-``port`` instance.

    Parameters
    ----------
    port
        TCP port the listen will bind. The pidfile name is scoped to
        the port so an operator running multiple listens on different
        ports does not fight a single lock.
    lock_dir
        Directory holding the pidfile. Must exist (caller's
        responsibility — :mod:`cli_pkg/listen_cmds` ``mkdir -p`` s the
        default ``~/.scitex/agent-container/runtime/`` before calling).
        Passed in (rather than read from ``$HOME``) so tests can
        isolate without env juggling.

    Returns
    -------
    LockHandle
        Carries the open fd holding the flock + the pidfile path. The
        caller MUST keep the handle alive for the lifetime of the
        process (closing the fd releases the lock).

    Raises
    ------
    ListenAlreadyRunningError
        When another live process already holds the flock. The error
        message names the holding PID and the lock file path so the
        operator can both diagnose and clear the conflict without
        external tooling (``lsof`` / ``netstat`` / ``ss``).
    """
    pid_file = _pid_file_path(port, lock_dir)
    fd = os.open(str(pid_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        holding_pid = _read_holding_pid(fd)
        os.close(fd)
        raise ListenAlreadyRunningError(
            f"another sac listen is already running on port {port} "
            f"(holding PID {holding_pid}, lock file {pid_file}). "
            f"To take over: stop that process (`kill {holding_pid}`) "
            "and retry."
        ) from exc

    # We now hold the exclusive flock. Stamp our PID in the file body
    # so a future conflict can name us. Truncate first so a longer
    # prior body (e.g. crashed-process PID) doesn't leave trailing
    # bytes. Any write error here is loud — we hold the lock, so a
    # subsequent caller would still see *some* PID, but a structurally
    # bad write deserves to surface.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    return LockHandle(fd=fd, pid_file=pid_file)


def release_listen_lock(handle: LockHandle) -> None:
    """Release the flock + remove the pidfile.

    Best-effort: any error is swallowed because process-exit (with the
    fd still open) is the safe fallback — the kernel releases the
    flock unconditionally on close, and a leftover pidfile is just a
    stale diagnostic the next acquire will overwrite. We try to clean
    up explicitly so operators inspecting the runtime dir between
    listens don't see noise, but we never raise.
    """
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    except OSError:  # stx-allow: fallback (reason: best-effort cleanup; kernel-released-on-close is the durable contract)
        pass
    try:
        os.close(handle.fd)
    except (
        OSError
    ):  # stx-allow: fallback (reason: fd already closed / invalid — non-fatal)
        pass
    try:
        handle.pid_file.unlink()
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: already removed by a prior call)
        pass
    except OSError:  # stx-allow: fallback (reason: best-effort; a leftover pidfile is harmless — next acquire overwrites)
        pass


def default_lock_dir() -> Path:
    """Return the default lock dir, honouring ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``.

    The CLI uses this; tests pass an explicit ``lock_dir`` to avoid
    touching the operator's runtime. Caller is responsible for
    ``mkdir -p`` — :func:`acquire_listen_lock` does NOT create the
    directory (avoid surprising the operator with auto-created paths).

    THE ENV OVERRIDE IS A SAFETY INTERLOCK, NOT A CONVENIENCE. This used to
    hard-code ``Path.home()``, which made the docstring's "tests pass an
    explicit ``lock_dir``" a promise enforced by NOTHING: any test that reached
    a listen CLI path without threading one through silently resolved the
    OPERATOR'S REAL runtime dir. That is not a dirty-state annoyance, it is an
    OUTAGE — this directory holds the `sac listen` PIDFILE, and a test that
    writes or acts on it can stop/restart the live control plane, tearing down
    the in-memory a2a broker and DEAFENING EVERY AGENT'S INBOX AT ONCE.
    Ambient-$HOME safety cannot rest on every future test remembering.

    CI could not see this and never will: there is no fleet on a runner. But
    the RELEASE gate runs on a self-hosted node where ``$HOME`` is the
    operator's own, persistent, real home — so the blast radius is live.

    ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` is the SAME variable
    ``_runners._session_state.DEFAULT_STATE_ROOT`` reads: one runtime root, one
    override, one place for a test harness to redirect (SSOT — deliberately not
    a second, parallel mechanism). Resolved PER CALL rather than baked at
    import, so a harness that sets the env is never too late.
    """
    return Path(
        os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
            str(Path.home() / ".scitex" / "agent-container" / "runtime"),
        )
    )
