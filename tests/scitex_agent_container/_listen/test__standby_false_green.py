"""Regression tests: the standby loop must never call a DEAD holder healthy.

Operator incident 2026-07-14 — ``sac listen`` printed this, forever::

    # sac listen: holder PID 738982 on 127.0.0.1:7878 not answering health
      (1/2) — re-checking in 4.0s before taking over
    # sac listen: standing by behind healthy holder PID 738982 on 127.0.0.1:7878
    # sac listen: standing by behind healthy holder PID 738982 on 127.0.0.1:7878
    ... (forever)

…while PID 738982 answered nothing and the whole fleet was cut off from
the host. Note the shape: it SAW the failed check, then went back to
calling the same holder "healthy". Two defects produced that:

1. ``consecutive_unhealthy = 0`` was reset by ANY single lucky reply, so
   a FLAPPING holder — one that answers one probe in N, which is exactly
   what the live 7878 daemon does (HTTP 200 one minute, ``Connection
   refused`` the next; measured on this box) — could never accumulate the
   CONSECUTIVE failures the take-over threshold demands. The record of
   the failed check was erased by the next lucky reply.
2. ``_default_probe_health`` was ``_http_get(...) > 0``, so ANY HTTP
   status — including a 5xx from a daemon whose health route is erroring
   — read as a "healthy holder".

Both are one class of bug: a DECLARED state trusted as an OBSERVED one.
And an unbounded standby loop hides it forever instead of failing loud.

No-mocks (PA-306 / STX-NM001-003): every holder here is a REAL HTTP
server on a REAL loopback socket, always on an ephemeral kernel-assigned
port — these tests NEVER touch the live 7878 control plane.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import http.server
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._listen import _standby as std
from scitex_agent_container._listen._single_instance import (
    ListenAlreadyRunningError,
    LockHandle,
)

HOLDER_PID = 738982  # the operator's actual wedged holder


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    saved = getattr(std, name)
    setattr(std, name, value)
    try:
        yield
    finally:
        setattr(std, name, saved)


def _no_sleep(_secs: float) -> None:
    """No-op sleep seam — keeps the loop's cadence out of wall-clock."""


class _AlwaysContended:
    """``_acquire`` seam that NEVER hands over the lock.

    Models the holder keeping its flock: the loop can only escape by
    taking over or failing loud, never by the port quietly freeing itself.
    """

    def __call__(self, *, port: int, lock_dir: Path) -> LockHandle:
        raise ListenAlreadyRunningError("held")


class _StopAfter:
    """``_should_stop`` seam: False for ``false_calls`` polls, then True.

    A GUARD-RAIL, not part of the contract under test — it bounds a loop
    that (on the buggy code) would otherwise spin forever and hang CI.
    Reaching it means the loop never reached a decision on its own.
    """

    def __init__(self, false_calls: int) -> None:
        self._false_calls = false_calls
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._false_calls


@contextmanager
def _real_holder(status_sequence: list[int]) -> Iterator[int]:
    """Run a REAL HTTP server on an ephemeral loopback port.

    ``status_sequence`` is cycled: each ``/v1/health`` GET is answered
    with the next status. A ``0`` entry means "answer NOTHING" — the
    handler slams the connection shut without a response, which is what a
    wedged holder looks like on the wire. Yields the bound port.
    """
    seq = list(status_sequence)
    counter = {"i": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
            status = seq[counter["i"] % len(seq)]
            counter["i"] += 1
            if status == 0:
                self.close_connection = True
                return
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _takeovers_against(port: int, *, stop_after: int = 12) -> list[dict]:
    """Drive ``resolve_startup`` against the REAL holder on ``port``.

    The health probe is NOT swapped — the real one runs against the real
    socket. Returns the take-over calls the loop made; empty means it
    stood by behind the holder instead of ever acting on it.
    """
    takeovers: list[dict] = []
    with (
        _swap("_acquire", _AlwaysContended()),
        _swap("_take_over", lambda **kw: takeovers.append(kw) or ""),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: HOLDER_PID),
        _swap("_sleep", _no_sleep),
        _swap("_should_stop", _StopAfter(stop_after)),
        _swap("_log", lambda _m: None),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=port,
            lock_dir=Path("/nonexistent"),
            standby_interval=0.01,
            health_timeout=0.5,
            unhealthy_takeover_threshold=2,
        )
    return takeovers


def _failure_message(port: int, *, take_over_error: str) -> str:
    """Return the ``ListenTakeoverFailed`` message, or ``""`` if none was raised.

    The ``_should_stop`` guard-rail bounds the OLD (unbounded) loop so a
    non-failing implementation returns ``""`` instead of hanging CI.
    """
    with (
        _swap("_acquire", _AlwaysContended()),
        _swap("_take_over", lambda **_kw: take_over_error),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: HOLDER_PID),
        _swap("_sleep", _no_sleep),
        _swap("_should_stop", _StopAfter(40)),
        _swap("_log", lambda _m: None),
    ):
        try:
            std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=0.01,
                health_timeout=0.5,
                unhealthy_takeover_threshold=2,
            )
        except std.ListenTakeoverFailed as exc:
            return str(exc)
    return ""


# ---------------------------------------------------------------------------
# Defect 1 — a FLAPPING holder must not have its failure record erased
# ---------------------------------------------------------------------------


def test_flapping_holder_is_eventually_taken_over() -> None:
    # Arrange — the operator's exact holder: it misses a health check, then
    # answers, then misses… The old loop wiped ``consecutive_unhealthy`` on
    # every lucky 200, so the corroboration threshold was NEVER reached and
    # it stood by behind a dead holder forever.
    with _real_holder([0, 200, 0, 200, 0, 200, 0, 200]) as port:
        # Act
        takeovers = _takeovers_against(port)
    # Assert
    assert takeovers, (
        "the standby loop never took over a holder that repeatedly failed "
        "/v1/health — one lucky reply erased the record of the failure"
    )


def test_flapping_holder_never_logged_as_healthy() -> None:
    # Arrange — a holder that has FAILED a check must never afterwards be
    # rendered to the operator as a "healthy holder". That log line is the
    # false-green he sat and watched scroll while the fleet was cut off.
    logs: list[str] = []
    with _real_holder([0, 200, 0, 200, 0, 200]) as port:
        # Act
        with (
            _swap("_acquire", _AlwaysContended()),
            _swap("_take_over", lambda **_kw: ""),
            _swap("_heal_untracked_port", lambda **_k: ""),
            _swap("_read_pid", lambda _p: HOLDER_PID),
            _swap("_sleep", _no_sleep),
            _swap("_should_stop", _StopAfter(8)),
            _swap("_log", logs.append),
        ):
            std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=0.01,
                health_timeout=0.5,
                unhealthy_takeover_threshold=2,
            )
    # Assert
    assert not any("healthy holder" in line for line in logs), (
        f"a holder that failed its health check was still announced as a "
        f"'healthy holder': {logs!r}"
    )


# ---------------------------------------------------------------------------
# Defect 2 — an answering-but-erroring holder is NOT healthy
# ---------------------------------------------------------------------------


def test_server_error_holder_is_taken_over() -> None:
    # Arrange — the holder answers every /v1/health with 503: bound and
    # speaking HTTP, but NOT serving. The old probe was ``_http_get() > 0``,
    # so a 503 read as a healthy holder — forever.
    with _real_holder([503]) as port:
        # Act
        takeovers = _takeovers_against(port, stop_after=10)
    # Assert
    assert takeovers, (
        "a holder answering /v1/health with 503 was treated as healthy — any "
        "HTTP status counted as health, so a broken daemon was never taken over"
    )


def test_auth_gated_holder_is_never_taken_over() -> None:
    # Arrange — the load-bearing counterpart (card
    # sac-listen-restart-healthcheck-bearer / PR #463): a 401 PROVES the
    # daemon is up and auth-gating. A false-RED here would destroy a
    # perfectly healthy control plane — strictly worse than the false-green
    # this PR fixes. It must NEVER be taken over.
    with _real_holder([401]) as port:
        # Act
        takeovers = _takeovers_against(port, stop_after=10)
    # Assert
    assert takeovers == []


def test_serving_holder_is_never_taken_over() -> None:
    # Arrange — the warm-standby path must survive the fix: a holder that
    # answers 200 every time is a working primary. Standing by behind it is
    # the FEATURE (#640); killing it would BE the outage.
    with _real_holder([200]) as port:
        # Act
        takeovers = _takeovers_against(port, stop_after=10)
    # Assert
    assert takeovers == []


# ---------------------------------------------------------------------------
# Defect 3 — the loop must be BOUNDED: take over, or FAIL LOUD
# ---------------------------------------------------------------------------


def test_unfreeable_holder_fails_loud_instead_of_spinning() -> None:
    # Arrange — the holder never answers AND the take-over can never free
    # the port. The old loop logged "standing by, will retry" and span
    # forever, silently, while the operator watched.
    with _real_holder([0]) as port:
        # Act
        message = _failure_message(port, take_over_error="port still held")
    # Assert — it terminated with a loud error instead of hiding the outage.
    assert message, "the standby loop span forever instead of failing loud"


def test_takeover_failure_names_the_holder_pid() -> None:
    # Arrange — the message must name the PID a human has to act on.
    with _real_holder([0]) as port:
        # Act
        message = _failure_message(port, take_over_error="port still held")
    # Assert
    assert str(HOLDER_PID) in message


def test_takeover_failure_names_the_exact_remedy() -> None:
    # Arrange — "a message a human can act on": the exact command to run.
    with _real_holder([0]) as port:
        # Act
        message = _failure_message(port, take_over_error="port still held")
    # Assert
    assert "sac listen restart --force" in message


def test_takeover_failure_names_the_health_evidence() -> None:
    # Arrange — the message must state what was OBSERVED (the holder did
    # not answer /v1/health), never merely assert a conclusion.
    with _real_holder([0]) as port:
        # Act
        message = _failure_message(port, take_over_error="port still held")
    # Assert
    assert "/v1/health" in message


# ---------------------------------------------------------------------------
# Never destroy on a negative signal
# ---------------------------------------------------------------------------


def test_takeover_never_uses_the_sigkill_escalator(tmp_path: Path) -> None:
    # Arrange — a probe-based "wedged" verdict must never SIGKILL: the
    # false-RED is worse than the false-green. The holder gets a graceful
    # SIGTERM and nothing more; if it ignores that, the human decides.
    escalations: list[int] = []
    (tmp_path / "listen-7999.pid").write_text(f"{HOLDER_PID}\n")
    # Act
    with (
        _swap("_terminate", lambda pid, **_kw: escalations.append(pid) or False),
        _swap("_terminate_graceful", lambda _pid, **_kw: True),
        _swap("pid_alive", lambda _p: True),
        _swap("_sleep", _no_sleep),
    ):
        std._default_take_over(
            host="127.0.0.1",
            port=7999,
            lock_dir=tmp_path,
            holder_pid=HOLDER_PID,
            grace_secs=0.05,
        )
    # Assert — the TERM→SIGKILL escalator was never reached.
    assert escalations == []


def test_surviving_holder_keeps_its_pidfile(tmp_path: Path) -> None:
    # Arrange — the old take-over unlinked the pidfile even when the holder
    # was still ALIVE. That (a) let a second daemon start beside a live one
    # and (b) broke the flock's atomic-bind invariant: a racing standby
    # holding an flock on the OLD inode and a new acquirer creating a FRESH
    # inode would BOTH believe they held "the lock".
    pidfile = tmp_path / "listen-7999.pid"
    pidfile.write_text(f"{HOLDER_PID}\n")
    # Act — the holder ignores SIGTERM and stays alive.
    with (
        _swap("_terminate_graceful", lambda _pid, **_kw: False),
        _swap("pid_alive", lambda _p: True),
        _swap("_sleep", _no_sleep),
    ):
        std._default_take_over(
            host="127.0.0.1",
            port=7999,
            lock_dir=tmp_path,
            holder_pid=HOLDER_PID,
            grace_secs=0.05,
        )
    # Assert — a LIVE holder's pidfile is left exactly where it was.
    assert pidfile.exists()


def test_surviving_holder_surfaces_loud_error(tmp_path: Path) -> None:
    # Arrange — a holder that ignores SIGTERM is NOT escalated; the
    # take-over reports a loud, actionable failure to the caller instead.
    pidfile = tmp_path / "listen-7999.pid"
    pidfile.write_text(f"{HOLDER_PID}\n")
    # Act
    with (
        _swap("_terminate_graceful", lambda _pid, **_kw: False),
        _swap("pid_alive", lambda _p: True),
        _swap("_sleep", _no_sleep),
    ):
        error = std._default_take_over(
            host="127.0.0.1",
            port=7999,
            lock_dir=tmp_path,
            holder_pid=HOLDER_PID,
            grace_secs=0.05,
        )
    # Assert
    assert "sac listen restart --force" in error


def test_dead_holder_takeover_reports_success(tmp_path: Path) -> None:
    # Arrange — the ordinary failover: the holder exits on SIGTERM, the
    # kernel releases its flock, and the loop re-acquires. No error.
    (tmp_path / "listen-7999.pid").write_text(f"{HOLDER_PID}\n")
    # Act
    with (
        _swap("_terminate_graceful", lambda _pid, **_kw: True),
        _swap("pid_alive", lambda _p: True),
        _swap("_sleep", _no_sleep),
    ):
        error = std._default_take_over(
            host="127.0.0.1",
            port=7999,
            lock_dir=tmp_path,
            holder_pid=HOLDER_PID,
            grace_secs=0.05,
        )
    # Assert
    assert error == ""


# ---------------------------------------------------------------------------
# The stop signal still wins (systemctl stop during standby stays clean)
# ---------------------------------------------------------------------------


def test_stop_signal_still_exits_without_binding() -> None:
    # Arrange — a SIGTERM landing inside the bounded re-check window (the
    # only place that waits at all) must be a prompt clean exit: no bind,
    # no take-over, no exception.
    with _real_holder([0]) as port:
        # Act
        with (
            _swap("_acquire", _AlwaysContended()),
            _swap("_take_over", lambda **_kw: ""),
            _swap("_read_pid", lambda _p: HOLDER_PID),
            _swap("_sleep", _no_sleep),
            _swap("_should_stop", _StopAfter(1)),
            _swap("_log", lambda _m: None),
        ):
            result = std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=0.01,
                health_timeout=0.5,
            )
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Defect 0 — a HEALTHY holder must make `sac listen` STAND DOWN, not spin
# ---------------------------------------------------------------------------


def test_serving_holder_stands_down_immediately() -> None:
    # Arrange — the operator's second incident. `sac listen` behind a
    # perfectly healthy holder printed "standing by behind healthy holder"
    # every 4s FOREVER; the only escape was Ctrl-C, and then he had to
    # `kill` the holder BY HAND. "Someone else is already serving" is a
    # SUCCESS: return, so the human gets his shell back.
    with _real_holder([200]) as port:
        # Act
        with (
            _swap("_acquire", _AlwaysContended()),
            _swap("_read_pid", lambda _p: 745734),
            _swap("_sleep", _no_sleep),
            _swap("_log", lambda _m: None),
        ):
            result = std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=0.01,
                health_timeout=0.5,
            )
    # Assert — decided, and returned. No spin, no bind.
    assert result is None


def test_serving_holder_never_polls_again() -> None:
    # Arrange — THE anti-spin assertion, against a REAL healthy holder: it
    # must be probed once and decided on. Every extra poll was another
    # "standing by behind healthy holder" line scrolling past the operator.
    sleeps: list[float] = []
    with _real_holder([200]) as port:
        # Act
        with (
            _swap("_acquire", _AlwaysContended()),
            _swap("_read_pid", lambda _p: 745734),
            _swap("_sleep", sleeps.append),
            _swap("_log", lambda _m: None),
        ):
            std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=4.0,
                health_timeout=0.5,
            )
    # Assert — it never waited, so it cannot have looped.
    assert sleeps == []


def test_serving_holder_reports_nothing_to_do() -> None:
    # Arrange — the operator asked why he had to kill by hand. The answer
    # has to be on screen: who is serving, and that there is nothing to do.
    logs: list[str] = []
    with _real_holder([200]) as port:
        # Act
        with (
            _swap("_acquire", _AlwaysContended()),
            _swap("_read_pid", lambda _p: 745734),
            _swap("_sleep", _no_sleep),
            _swap("_log", logs.append),
        ):
            std.resolve_startup(
                host="127.0.0.1",
                port=port,
                lock_dir=Path("/nonexistent"),
                standby_interval=0.01,
                health_timeout=0.5,
            )
    # Assert
    assert any("already serving: PID 745734" in line for line in logs)


def test_takeover_failed_is_a_runtime_error() -> None:
    # Arrange — ListenTakeoverFailed must be a distinct, catchable type so
    # the CLI can map it to a non-zero exit carrying the operator's message.
    failure_type = std.ListenTakeoverFailed
    # Act
    is_runtime_error = issubclass(failure_type, RuntimeError)
    # Assert
    assert is_runtime_error


def test_takeover_failed_is_not_already_running() -> None:
    # Arrange — the two must not be conflated: contention is NORMAL (stand
    # by), an unfreeable wedged holder is an OUTAGE (fail loud).
    failure_type = std.ListenTakeoverFailed
    # Act
    conflated = issubclass(failure_type, ListenAlreadyRunningError)
    # Assert
    assert not conflated


@pytest.mark.parametrize("status", [200, 204, 401, 403, 404])
def test_answering_holder_is_not_taken_over(status: int) -> None:
    # Arrange — every sub-500 answer PROVES the daemon is bound and
    # speaking HTTP. None of them may trigger a destroy.
    with _real_holder([status]) as port:
        # Act
        takeovers = _takeovers_against(port, stop_after=8)
    # Assert
    assert takeovers == []
