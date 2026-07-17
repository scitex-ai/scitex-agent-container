"""Spawn and bound ONE ``host_exec`` child.

Split out of :mod:`._host_exec` (which kept validation + ACL + audit + the
handler) because the semantics here are subtle enough to be worth reading on
their own: the process-group kill and the stdin closure are the two guards that
make a stuck child survivable, and both rest on measured POSIX behaviour rather
than on the docs' plain reading. See :mod:`._host_exec` for the incident this
came from.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass

# After SIGKILLing the child's process GROUP, how long to drain its pipes. The
# group is dead, so EOF is immediate in practice; this is bounded anyway
# because a grandchild that escaped the group (its own setsid) could still hold
# the write end, and an unbounded drain here would be the original bug again.
_POST_KILL_DRAIN_S: float = 5.0


@dataclass(frozen=True)
class ChildOutcome:
    """Result of one brokered child. Typed so a timeout can NEVER be confused
    with success-with-no-output — the distinction the incident turned on."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    killed_process_group: bool


def _kill_process_group(proc: subprocess.Popen) -> bool:
    """SIGKILL the child's whole process GROUP. Returns True if the group was
    signalled.

    ``proc.kill()`` signals ONLY the direct child, so grandchildren survive and
    keep running (measured: ``bash -c 'sleep 60 & cat'`` left ``sleep`` alive
    after the documented timeout fired). The child is spawned with
    ``start_new_session=True``, so its pid IS its process-group id and the
    group contains everything it spawned.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # Already reaped, or not ours — fall back to the direct child.
        proc.kill()
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        proc.kill()
        return False


def _run_child(
    argv: list[str],
    *,
    cwd: str | None,
    child_timeout_s: float,
    env: dict[str, str] | None,
) -> ChildOutcome:
    """Run one child to completion, bounded. Blocking — call OFF the loop.

    Runs in its own session/process group with stdin closed; on timeout the
    whole GROUP is killed and the (bounded) drain collects whatever it wrote.

    ``stdin=DEVNULL`` kills the "child blocks on stdin" class by construction
    (``git`` without ``-F <file>``, an ssh passphrase prompt, ``apt``, a pager,
    ``read``): measured, such a child gets EOF in 0.02s instead of hanging. It
    does NOT break the ``echo <b64> | base64 -d | bash`` delivery shape — that
    outer bash takes its script from ``-c``, and the inner pipeline builds its
    own stdin internally; both forms were verified to deliver identically. No
    caller can send stdin anyway: the request body has no ``stdin`` field.

    The parameter is ``child_timeout_s``, not ``timeout_s``, deliberately: this
    is dispatched through ``run_blocking``, which consumes ``timeout_s`` as its
    OWN watchdog deadline. Same name would silently hand the child's deadline
    to the watchdog and leave the child unbounded.
    """
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=child_timeout_s)
            return ChildOutcome(
                exit_code=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
                timed_out=False,
                killed_process_group=False,
            )
        except subprocess.TimeoutExpired:
            killed = _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=_POST_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                # A grandchild escaped the group and still holds the pipe. The
                # child is dead; report without its output rather than block.
                stdout, stderr = "", ""
            return ChildOutcome(
                exit_code=-1,
                stdout=stdout or "",
                stderr=stderr or "",
                timed_out=True,
                killed_process_group=killed,
            )


__all__ = ["ChildOutcome", "_run_child", "_POST_KILL_DRAIN_S"]
