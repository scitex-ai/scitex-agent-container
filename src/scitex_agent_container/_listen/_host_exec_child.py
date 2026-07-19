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
import time
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


# How long the group has to run its own cleanup after SIGTERM, before SIGKILL.
#
# INCIDENT 2026-07-18 — stranded `.git/index.lock`. This path used to send a
# bare SIGKILL, which cannot be caught, so a child died with its cleanup
# handlers unrun. Measured by strace: `git status --porcelain` really does
# `openat(".git/index.lock", O_RDWR|O_CREAT|O_EXCL)`, so a brokered git op that
# overran its timeout stranded a lock EVERY time. One such lock left the
# once-a-minute post-merge-pull sweep failing for 83 minutes — and a stranded
# lock is silent to readers (`git status` still exits 0), so it sits invisible
# until something needs to WRITE.
#
# 3s is chosen to cover a lock-file unlink (microseconds) with room to spare,
# while staying far below `_WATCHDOG_MARGIN_S` in `._host_exec`, which is
# derived from this constant so the two can never drift apart.
_TERM_GRACE_S: float = 3.0


def _terminate_process_group(
    proc: subprocess.Popen, *, grace_s: float = _TERM_GRACE_S
) -> bool:
    """SIGTERM the child's process GROUP, allow a bounded grace for cleanup,
    then SIGKILL the group. Returns True if the group was signalled.

    SIGTERM FIRST because SIGKILL is uncatchable: under a bare SIGKILL a child
    never runs its cleanup, which is how `git`'s own lock removal was skipped
    and `.git/index.lock` files were stranded on the shared fleet checkouts.

    SIGKILL STILL FOLLOWS, unconditionally, because the grace is a courtesy and
    not a veto — a child that ignores SIGTERM must not be able to outlive its
    deadline, which was the unbounded-child wedge of INCIDENT 2026-07-17.

    The grace is a FLAT sleep rather than a wait-for-exit, deliberately.
    `proc.wait()` REAPS the child, freeing its pid — and the pid IS the pgid
    (`start_new_session=True`), so a reaped-then-recycled pgid would point our
    SIGKILL at an unrelated process group. Leaving the child unreaped here
    keeps the pgid reserved by its own zombie until `communicate()` collects
    it. The cost is that a timed-out call always spends the full grace; that is
    an already-exceptional path which has just spent `timeout_s` seconds, so a
    few more are noise. Do not "optimise" this into a wait() without resolving
    the recycle hazard.

    The SIGKILL is also why the direct child exiting on SIGTERM is not enough
    to stop here: it says nothing about GRANDCHILDREN, and the process-group
    kill exists precisely because grandchildren outlive their parent (measured:
    `bash -c 'sleep 60 & cat'` left `sleep` alive).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # Already reaped, or not ours — fall back to the direct child.
        proc.kill()
        return False

    signalled = False
    try:
        os.killpg(pgid, signal.SIGTERM)
        signalled = True
    except (ProcessLookupError, PermissionError):
        # Nothing to term (already gone) — the SIGKILL sweep below still runs.
        pass

    if signalled and grace_s > 0:
        time.sleep(grace_s)

    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        # Whole group is already gone: SIGTERM was sufficient. Still a kill.
        return signalled
    except PermissionError:
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
    whole GROUP is SIGTERMed, given a bounded grace to run its own cleanup, and
    then SIGKILLed, after which the (bounded) drain collects whatever it wrote.

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
            killed = _terminate_process_group(proc)
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


__all__ = [
    "ChildOutcome",
    "_run_child",
    "_POST_KILL_DRAIN_S",
    "_TERM_GRACE_S",
    "_terminate_process_group",
]
