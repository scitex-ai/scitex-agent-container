"""Tests for the TUI turn-bridge port machinery (``_tui_turn_bridge_port``).

This is the restart port-collision fix (2026-07-09): the OLD bridge still
held the a2a port when the new one booted, so the child crashed with
``OSError [Errno 98] Address already in use`` and the agent was stranded.
The guards here make ``restart`` reliably RELEASE + REBIND the port.

Real seams only (PA-306 no-mocks): the bind checks run against REAL sockets
on ephemeral ports; the release path SIGKILLs a REAL listener subprocess; a
fake clock (plain callables, not a mock) drives the bounded waits without
real time. STX-TQ002 AAA markers + STX-TQ007 one observable assert +
STX-TQ003 descriptive names.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Callable, Iterator

import pytest

from scitex_agent_container.runtimes import _tui_turn_bridge_port as portmod

_HOST = "127.0.0.1"

# The force-kill sweep resolves the holder PID via lsof/ss/fuser; skip the
# real-survivor test on a bare host that ships none of them (the parsing +
# fallback chain is covered by ``tests/.../_listen/test__port_holder.py``).
_HAS_PORT_DISCOVERY = bool(
    shutil.which("lsof") or shutil.which("ss") or shutil.which("fuser")
)


# ---------------------------------------------------------------------------
# Helpers — real ephemeral ports + a real listener subprocess
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Return a currently-free ephemeral TCP port (bind :0, read, release)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((_HOST, 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


class _FakeClock:
    """Deterministic monotonic clock — ``now``/``sleep`` are plain callables."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def listeners() -> Iterator[Callable[[int], subprocess.Popen]]:
    """Spawn REAL listener subprocesses on a port; tear them all down."""
    procs: list[subprocess.Popen] = []

    def start(port: int) -> subprocess.Popen:
        code = (
            "import socket,time;"
            "s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('{_HOST}',{port}));"
            "s.listen();"
            "time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])
        procs.append(proc)
        # Wait until it has actually bound (port no longer free) before use.
        portmod.poll_until(
            lambda: not portmod.port_is_free(_HOST, port),
            timeout_s=5.0,
            poll_s=0.05,
            sleep_fn=time.sleep,
            now_fn=time.monotonic,
        )
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# port_is_free — the faithful "will the new bridge bind here?" probe
# ---------------------------------------------------------------------------
def test_port_is_free_true_on_unbound_port() -> None:
    # Arrange
    port = _free_port()
    # Act
    free = portmod.port_is_free(_HOST, port)
    # Assert
    assert free is True


def test_port_is_free_false_while_listener_bound_true_after_close() -> None:
    # Arrange — a REAL bound+listening socket holds the port.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind((_HOST, 0))
    held.listen()
    port = int(held.getsockname()[1])
    # Act
    while_bound = portmod.port_is_free(_HOST, port)
    held.close()
    after_close = portmod.port_is_free(_HOST, port)
    # Assert — held → not free; closed → bindable again (SO_REUSEADDR rebind).
    assert (while_bound, after_close) == (False, True)


# ---------------------------------------------------------------------------
# poll_until — bounded wait with injected clock
# ---------------------------------------------------------------------------
def test_poll_until_true_when_predicate_flips_before_timeout() -> None:
    # Arrange — predicate flips True on the 3rd call.
    clock = _FakeClock()
    calls = {"n": 0}

    def predicate() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    # Act
    ok = portmod.poll_until(
        predicate, timeout_s=10.0, poll_s=0.2, sleep_fn=clock.sleep, now_fn=clock.now
    )
    # Assert
    assert ok is True


def test_poll_until_false_on_timeout() -> None:
    # Arrange — predicate never holds; the fake clock advances past deadline.
    clock = _FakeClock()
    # Act
    ok = portmod.poll_until(
        lambda: False,
        timeout_s=0.5,
        poll_s=0.2,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
    )
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# port_busy_error — the actionable fail-loud message
# ---------------------------------------------------------------------------
def test_port_busy_error_carries_port() -> None:
    # Arrange
    port = _free_port()
    # Act
    err = portmod.port_busy_error(_HOST, port, "figrecipe")
    # Assert
    assert err.port == port


def test_port_busy_error_names_port_and_remediation() -> None:
    # Arrange
    port = _free_port()
    # Act
    msg = str(portmod.port_busy_error(_HOST, port, "figrecipe"))
    # Assert — the operator gets the port + the exact one-liner remediation.
    assert (
        str(port) in msg
        and f"fuser -k {port}/tcp" in msg
        and "tmux kill-session -t tui-figrecipe" in msg
        and "sac agents restart figrecipe" in msg
    )


# ---------------------------------------------------------------------------
# ensure_port_free_or_raise — the pre-spawn gate
# ---------------------------------------------------------------------------
def test_ensure_port_free_or_raise_returns_when_free() -> None:
    # Arrange — a free port; the gate must return quietly (no raise).
    port = _free_port()
    clock = _FakeClock()
    # Act
    portmod.ensure_port_free_or_raise(
        host=_HOST,
        port=port,
        agent_name="figrecipe",
        timeout_s=1.0,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
    )
    # Assert — reaching here (no exception) is the observable success.
    assert portmod.port_is_free(_HOST, port) is True


def test_ensure_port_free_or_raise_fails_loud_when_held(listeners) -> None:
    # Arrange — a REAL listener keeps the port bound for the whole wait.
    port = _free_port()
    listeners(port)
    clock = _FakeClock()
    # Act
    # Assert
    with pytest.raises(portmod.TurnBridgePortBusyError):
        portmod.ensure_port_free_or_raise(
            host=_HOST,
            port=port,
            agent_name="figrecipe",
            timeout_s=0.5,
            sleep_fn=clock.sleep,
            now_fn=clock.now,
        )


# ---------------------------------------------------------------------------
# await_bridge_release — SIGKILL escalation frees the port
# ---------------------------------------------------------------------------
def test_await_bridge_release_sigkills_overstaying_holder(listeners) -> None:
    # Arrange — a REAL listener that never gives up the port on its own; the
    # release wait must SIGKILL it after the grace and see the port freed.
    # Real time here (not the fake clock) so the kernel gets real time to reap
    # the SIGKILLed holder and close its socket — no race on the re-probe.
    port = _free_port()
    proc = listeners(port)
    # Act
    released = portmod.await_bridge_release(
        proc.pid,
        port,
        host=_HOST,
        grace_s=0.3,
        sleep_fn=time.sleep,
        now_fn=time.monotonic,
    )
    proc.wait(timeout=5)
    # Assert — the holder was killed and the port is bindable again.
    assert released is True and portmod.port_is_free(_HOST, port) is True


def test_await_bridge_release_returns_true_when_already_free() -> None:
    # Arrange — port already free + an already-dead pid; must return without
    # signalling anything.
    port = _free_port()
    clock = _FakeClock()
    # Act
    released = portmod.await_bridge_release(
        999_999,
        port,
        host=_HOST,
        grace_s=0.4,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
    )
    # Assert
    assert released is True


# ---------------------------------------------------------------------------
# _pid_alive — liveness fallback
# ---------------------------------------------------------------------------
def test_pid_alive_true_for_self() -> None:
    # Arrange
    pid = os.getpid()
    # Act
    alive = portmod._pid_alive(pid)
    # Assert
    assert alive is True


def test_pid_alive_false_for_reaped_pid() -> None:
    # Arrange — a subprocess that has exited and been reaped.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    # Act
    alive = portmod._pid_alive(proc.pid)
    # Assert
    assert alive is False


# ---------------------------------------------------------------------------
# force_free_own_port — the STOP-path ``fuser -k`` for a survivor holder
# ---------------------------------------------------------------------------
@pytest.fixture
def sigterm_immune_listeners() -> Iterator[Callable[[int], subprocess.Popen]]:
    """Spawn REAL listeners that IGNORE SIGTERM; only SIGKILL frees them.

    Models the 2026-07-12 incident's survivor: a process still bound to the
    agent's a2a port that a plain SIGTERM (``await_bridge_release``'s first
    escalation) cannot reap — so ONLY the force-kill sweep frees the port.
    Torn down via SIGKILL (which cannot be ignored).
    """
    procs: list[subprocess.Popen] = []

    def start(port: int) -> subprocess.Popen:
        code = (
            "import socket,signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('{_HOST}',{port}));"
            "s.listen();"
            "time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])
        procs.append(proc)
        portmod.poll_until(
            lambda: not portmod.port_is_free(_HOST, port),
            timeout_s=5.0,
            poll_s=0.05,
            sleep_fn=time.sleep,
            now_fn=time.monotonic,
        )
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(
    not _HAS_PORT_DISCOVERY,
    reason="needs lsof/ss/fuser to resolve the port holder for the SIGKILL sweep",
)
def test_force_free_own_port_sigkills_sigterm_immune_survivor(
    sigterm_immune_listeners,
) -> None:
    # Arrange — a REAL survivor bound to the agent's OWN a2a port that IGNORES
    # SIGTERM; only the force-kill sweep (SIGKILL via the real port_holder_pids
    # finder) frees the port. Real time so the kernel reaps the killed holder's
    # socket before the re-probe.
    port = _free_port()
    proc = sigterm_immune_listeners(port)
    # Act
    portmod.force_free_own_port(
        port,
        host=_HOST,
        agent_name="figrecipe",
        grace_s=1.0,
        sleep_fn=time.sleep,
        now_fn=time.monotonic,
    )
    proc.wait(timeout=5)
    # Assert — the SIGTERM-immune survivor was SIGKILLed; the port is bindable.
    assert portmod.port_is_free(_HOST, port) is True


def test_force_free_own_port_noop_when_already_free() -> None:
    # Arrange — a free port; the sweep must return True without killing anything.
    port = _free_port()
    clock = _FakeClock()
    # Act
    freed = portmod.force_free_own_port(
        port,
        host=_HOST,
        agent_name="figrecipe",
        grace_s=0.5,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
    )
    # Assert
    assert freed is True


def test_force_free_own_port_returns_false_when_port_never_frees() -> None:
    # Arrange — a probe that never reports the port free (a genuinely
    # unkillable holder) drives the bounded re-poll to time out → False, so the
    # caller still FAILS LOUD instead of hiding a stuck port. The real port is
    # free, so the holder sweep finds nothing to kill.
    port = _free_port()
    clock = _FakeClock()
    # Act
    freed = portmod.force_free_own_port(
        port,
        host=_HOST,
        agent_name="figrecipe",
        grace_s=0.5,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        port_free_fn=lambda _h, _p: False,
    )
    # Assert
    assert freed is False
