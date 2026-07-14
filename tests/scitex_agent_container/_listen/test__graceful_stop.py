"""Tests for ``_listen/_graceful_stop.py`` — the take-over may ASK, never destroy.

The contract these pin: the standby loop's automatic take-over acts on a
PROBE-BASED inference. A probe can be wrong, and the remedy for a wrong
"wedged" verdict is to SIGKILL a healthy control plane and cut the whole
fleet off from the host. So the automatic path sends SIGTERM and NOTHING
ELSE — a survivor is reported to the caller, never escalated.

No-mocks (PA-306 / STX-NM001-003): these drive REAL child processes with
REAL signals. The "ignores SIGTERM" case installs ``SIG_IGN`` in a real
child, so a stray SIGKILL anywhere in the implementation would show up
as a dead process — which is exactly what the assertion checks.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from scitex_agent_container._listen._graceful_stop import terminate_gracefully

# Each child announces "ready" on stdout and the harness BLOCKS on that
# line before signalling it. Do NOT replace this with a sleep: under `-n 8`
# an interpreter can take longer than any guessed delay to install its
# handler, and a SIGTERM landing before ``SIG_IGN`` is in place kills the
# "stubborn" child — which would make the never-SIGKILL test pass or fail
# on a race rather than on the behaviour it claims to check.

# A child that sleeps until signalled — SIGTERM's default disposition kills
# it, so it models a well-behaved holder.
_OBEDIENT = (
    "import sys, time; sys.stdout.write('ready\\n'); sys.stdout.flush(); "
    "time.sleep(30)"
)
# A child that IGNORES SIGTERM outright — it models the wedged holder we
# must refuse to destroy. Only a SIGKILL could take it down, so if it is
# dead after terminate_gracefully, the implementation escalated.
_STUBBORN = (
    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); "
    "time.sleep(30)"
)


@contextmanager
def _child(code: str) -> Iterator[subprocess.Popen]:
    """Spawn a REAL child process running ``code``; always reap it.

    A background reaper is essential, not incidental: a dead-but-unreaped
    child lingers as a ZOMBIE, and ``os.kill(pid, 0)`` — the liveness probe
    ``terminate_gracefully`` polls — SUCCEEDS on a zombie. Without a reaper
    the harness would report a process that has already exited as still
    alive, and the test would "fail" on a liveness artefact of its own
    making. (Production never hits this: the flock holder is an independent
    daemon, never a child of ``sac listen``.)
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    # BLOCK until the child says it is ready — its signal handler is
    # installed by then. A guessed sleep is not enough under parallel load.
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    assert ready.strip() == "ready", f"child never became ready: {ready!r}"

    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        reaper.join(timeout=5)
        proc.stdout.close()


def _is_alive(pid: int) -> bool:
    """True iff the PID is still a running (non-reaped) process."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---------------------------------------------------------------------------
# The ordinary failover — a well-behaved holder exits on SIGTERM
# ---------------------------------------------------------------------------


def test_obedient_holder_is_stopped() -> None:
    # Arrange — a real child that dies on SIGTERM's default disposition.
    with _child(_OBEDIENT) as proc:
        # Act
        stopped = terminate_gracefully(proc.pid, grace_secs=5.0, poll_interval=0.1)
    # Assert
    assert stopped is True


def test_already_dead_holder_reports_stopped() -> None:
    # Arrange — the holder is already gone. That IS the goal state, so it
    # must not read as a failure (its flock is already kernel-released).
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    # Act
    stopped = terminate_gracefully(proc.pid, grace_secs=0.5, poll_interval=0.1)
    # Assert
    assert stopped is True


# ---------------------------------------------------------------------------
# Never destroy on a negative signal
# ---------------------------------------------------------------------------


def test_stubborn_holder_reports_not_stopped() -> None:
    # Arrange — a real child that IGNORES SIGTERM (the wedged holder).
    with _child(_STUBBORN) as proc:
        # Act
        stopped = terminate_gracefully(proc.pid, grace_secs=1.0, poll_interval=0.1)
    # Assert — we report the truth rather than pretend we stopped it.
    assert stopped is False


def test_stubborn_holder_is_never_killed() -> None:
    # Arrange — THE load-bearing test. The child ignores SIGTERM, so the
    # ONLY way it can end up dead is a SIGKILL. A probe-based verdict must
    # never be able to destroy a process: the false-RED (killing a healthy
    # control plane) is strictly worse than the false-green.
    with _child(_STUBBORN) as proc:
        terminate_gracefully(proc.pid, grace_secs=1.0, poll_interval=0.1)
        # Act
        survived = _is_alive(proc.pid)
    # Assert
    assert survived, "terminate_gracefully escalated to SIGKILL — it must not"


def test_stubborn_holder_is_bounded_by_grace() -> None:
    # Arrange — the wait must be BOUNDED. An unbounded wait on a wedged
    # holder is just the unbounded standby loop wearing a different hat.
    with _child(_STUBBORN) as proc:
        started = time.monotonic()
        # Act
        terminate_gracefully(proc.pid, grace_secs=1.0, poll_interval=0.1)
        elapsed = time.monotonic() - started
    # Assert — returned promptly after the grace window, not "eventually".
    assert elapsed < 5.0


def test_sigterm_is_the_only_signal_sent() -> None:
    # Arrange — pin the exact signal at the syscall seam, so a future edit
    # cannot quietly slip a SIGKILL back in.
    sent: list[int] = []
    from scitex_agent_container._listen import _graceful_stop as gs

    saved_kill, saved_sleep, saved_alive = gs._kill, gs._sleep, gs._alive
    gs._kill = lambda _pid, sig: sent.append(sig)
    gs._sleep = lambda _s: None
    gs._alive = lambda _p: False  # exits promptly after the TERM
    # Act
    try:
        terminate_gracefully(4242, grace_secs=1.0, poll_interval=0.1)
    finally:
        gs._kill, gs._sleep, gs._alive = saved_kill, saved_sleep, saved_alive
    # Assert
    assert sent == [signal.SIGTERM]
