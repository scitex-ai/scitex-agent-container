"""Tests for ``_listen/_standby.py`` — hot-standby + failover startup
(card ``sac-listen-hot-standby-no-crashloop``).

The bug: a second ``sac listen`` launched while one already holds the
port used to exit 1, which under systemd ``Restart=always`` was an
infinite CRASH-LOOP. ``resolve_startup`` turns the second instance into
a warm STANDBY that fails over instead of crashing.

The health verdict is now THREE-state (``HolderProbe``) rather than a
bool — see ``test__standby_false_green.py`` for the false-green
regression suite and ``_holder_health.py`` for why two states could not
express "I asked and got nothing".

No-mocks (PA-306 / STX-NM001-003): every external effect is a
module-level seam swapped via a hand-rolled save/restore context
manager. The health probe runs against a REAL loopback ``http.server``
and a REAL closed port; the signal guard is exercised with a REAL
handler + a REAL in-process ``SIGTERM`` — no MagicMock anywhere.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scitex_agent_container._listen import _standby as std
from scitex_agent_container._listen._holder_health import HolderHealth, HolderProbe
from scitex_agent_container._listen._single_instance import (
    ListenAlreadyRunningError,
    LockHandle,
)

SERVING = HolderProbe(health=HolderHealth.SERVING, status=200)
UNREACHABLE = HolderProbe(health=HolderHealth.UNREACHABLE, status=-1)


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    saved = getattr(std, name)
    setattr(std, name, value)
    try:
        yield
    finally:
        setattr(std, name, saved)


def _no_sleep(_secs: float) -> None:
    """No-op sleep seam."""


def _sentinel_handle() -> LockHandle:
    """A stand-in lock handle whose identity the serve-path asserts on."""
    return LockHandle(fd=-1, pid_file=Path("/nonexistent/listen.pid"))


class _FakeAcquire:
    """Raises ``ListenAlreadyRunningError`` for the first ``raises`` calls,
    then returns ``handle`` — models a contended port that later frees."""

    def __init__(self, *, raises: int, handle: LockHandle) -> None:
        self._remaining = raises
        self._handle = handle
        self.calls = 0

    def __call__(self, *, port: int, lock_dir: Path) -> LockHandle:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise ListenAlreadyRunningError("held")
        return self._handle


class _StopAfter:
    """`should_stop` seam: returns False for the first ``false_calls``
    consultations, then True — bounds an otherwise-infinite standby loop."""

    def __init__(self, false_calls: int) -> None:
        self._false_calls = false_calls
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._false_calls


class _GracefulRecorder:
    """Records each graceful-stop call; reports the holder as exited."""

    def __init__(self, *, died: bool = True) -> None:
        self.calls: list[int] = []
        self._died = died

    def __call__(self, pid: int, *, grace_secs: float) -> bool:
        self.calls.append(pid)
        return self._died


# ---------------------------------------------------------------------------
# resolve_startup — port free / holder released → serve
# ---------------------------------------------------------------------------


def test_port_free_acquires_and_serves() -> None:
    # Arrange — the flock acquires on the first try (port free / prior
    # holder died and the kernel released its flock).
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=0, handle=sentinel)
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        result = std.resolve_startup(
            host="127.0.0.1", port=7878, lock_dir=Path("/tmp")
        )
    # Assert — the held handle is returned for the caller to serve with.
    assert result is sentinel


# ---------------------------------------------------------------------------
# resolve_startup — serving holder → STAND DOWN (exit 0). Never spin.
# ---------------------------------------------------------------------------


def test_serving_holder_stands_down_immediately() -> None:
    # Arrange — another instance is already serving. That is a SUCCESS: the
    # goal state ("a listen is serving on this port") is already true, so
    # there is nothing to do. Returning None tells the caller to exit 0
    # WITHOUT binding. It must NOT poll the holder forever — that spinner is
    # what forced the operator to Ctrl-C and `kill` the holder by hand.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: SERVING),
        _swap("_read_pid", lambda _p: 745734),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        result = std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert — stood down rather than binding or spinning.
    assert result is None


def test_serving_holder_probed_exactly_once() -> None:
    # Arrange — THE anti-spin assertion. The old loop re-probed a healthy
    # holder every 4s, without end. One probe is enough to decide.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    probes: list[int] = []

    def _probe(*_a, **_k):
        probes.append(1)
        return SERVING

    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", _probe),
        _swap("_read_pid", lambda _p: 745734),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert
    assert len(probes) == 1


def test_serving_holder_never_sleeps() -> None:
    # Arrange — an interactive `sac listen` behind a healthy holder must
    # RETURN, not wait. Any sleep at all on this path is a spin.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    sleeps: list[float] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: SERVING),
        _swap("_read_pid", lambda _p: 745734),
        _swap("_sleep", sleeps.append),
        _swap("_log", lambda _m: None),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=4.0,
        )
    # Assert
    assert sleeps == []


def test_already_serving_log_names_the_pid() -> None:
    # Arrange — the operator must be told WHO is serving, so he can decide
    # whether he wanted that process running at all.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    logs: list[str] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: SERVING),
        _swap("_read_pid", lambda _p: 745734),
        _swap("_sleep", _no_sleep),
        _swap("_log", logs.append),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert
    assert any("already serving: PID 745734" in line for line in logs)


def test_already_serving_log_states_the_evidence() -> None:
    # Arrange — the line must report what the holder ACTUALLY did, rather
    # than claim a bare "healthy holder" conclusion. That wording is the
    # false-green the operator watched scroll (incident 2026-07-14).
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    logs: list[str] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: SERVING),
        _swap("_read_pid", lambda _p: 745734),
        _swap("_sleep", _no_sleep),
        _swap("_log", logs.append),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert
    assert any("HTTP 200" in line for line in logs)


def test_serving_holder_never_takes_over() -> None:
    # Arrange — a serving holder must NEVER be killed. Contended once,
    # then frees; the take-over seam must remain untouched.
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=1, handle=sentinel)
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: SERVING),
        _swap("_take_over", lambda **kw: takeovers.append(kw) or ""),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: 4242),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        std.resolve_startup(
            host="127.0.0.1", port=7878, lock_dir=Path("/tmp"), standby_interval=0.01
        )
    # Assert
    assert takeovers == []


# ---------------------------------------------------------------------------
# resolve_startup — wedged holder → TAKE OVER after corroboration
# ---------------------------------------------------------------------------


def test_wedged_holder_takes_over_then_serves() -> None:
    # Arrange — contended behind a holder that never answers health; after
    # the corroboration threshold the take-over frees the port and the
    # re-acquire serves.
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=2, handle=sentinel)
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: UNREACHABLE),
        _swap("_take_over", lambda **kw: takeovers.append(kw) or ""),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: 4242),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        result = std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
            unhealthy_takeover_threshold=2,
        )
    # Assert — exactly one take-over, then served.
    assert result is sentinel and len(takeovers) == 1


def test_single_unhealthy_miss_does_not_take_over() -> None:
    # Arrange — one missed health probe then the holder answers again. A
    # single miss must NOT trigger a take-over (a holder that just won the
    # flock may not have finished binding). Threshold is 2.
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=2, handle=sentinel)
    health = iter([UNREACHABLE, SERVING])
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: next(health, SERVING)),
        _swap("_take_over", lambda **kw: takeovers.append(kw) or ""),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: 4242),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
            unhealthy_takeover_threshold=2,
        )
    # Assert — the recovered holder was never taken over.
    assert takeovers == []


# ---------------------------------------------------------------------------
# resolve_startup — SIGTERM during standby → clean exit, no bind
# ---------------------------------------------------------------------------


def test_stop_signal_during_recheck_exits_without_binding() -> None:
    # Arrange — a SUSPECT holder (so we are inside the bounded re-check
    # window, the only place that waits at all); a stop is flagged on the
    # first re-check pause. ``systemctl stop`` / Ctrl-C must be a prompt
    # clean exit that never binds and never takes over.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: UNREACHABLE),
        _swap("_take_over", lambda **kw: takeovers.append(kw) or ""),
        _swap("_read_pid", lambda _p: 4242),
        _swap("_sleep", _no_sleep),
        _swap("_should_stop", _StopAfter(1)),
        _swap("_log", lambda _m: None),
    ):
        result = std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert — clean exit signalled by None.
    assert result is None


# ---------------------------------------------------------------------------
# resolve_startup — unkillable port remnant on the serve path
# ---------------------------------------------------------------------------


def test_unkillable_port_remnant_releases_lock_and_retries() -> None:
    # Arrange — the flock acquires but an untracked remnant holds the port
    # and cannot be freed. Rather than crash-loop into EADDRINUSE, the
    # loop must RELEASE the flock and retry (here we then stop).
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=0, handle=sentinel)
    releases: list[LockHandle] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_heal_untracked_port", lambda **_k: "port 7878 still held"),
        _swap("_release", releases.append),
        _swap("_should_stop", _StopAfter(1)),
        _swap("_sleep", _no_sleep),
        _swap("_log", lambda _m: None),
    ):
        result = std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert — the flock was released (not held into a crash) and we exited.
    assert result is None and releases == [sentinel]


# ---------------------------------------------------------------------------
# _sleep_unless_stopped — interruptible standby wait
# ---------------------------------------------------------------------------


def test_sleep_unless_stopped_bails_immediately_when_flagged() -> None:
    # Arrange — the stop flag is already set; the wait must not sleep.
    sleeps: list[float] = []
    # Act
    with _swap("_should_stop", lambda: True), _swap("_sleep", sleeps.append):
        std._sleep_unless_stopped(10.0)
    # Assert — zero sleeps: the wait never blocked the (pending) signal.
    assert sleeps == []


def test_sleep_unless_stopped_sleeps_in_slices_when_running() -> None:
    # Arrange — not stopped; a 0.5s wait subdivides into 0.25s slices.
    sleeps: list[float] = []
    # Act
    with _swap("_should_stop", lambda: False), _swap("_sleep", sleeps.append):
        std._sleep_unless_stopped(0.5)
    # Assert — two 0.25s slices span the interval.
    assert len(sleeps) == 2


# ---------------------------------------------------------------------------
# standby_signal_guard — real signal wiring + handler restoration
# ---------------------------------------------------------------------------


def test_signal_guard_installed_handler_trips_stop_flag() -> None:
    # Arrange — invoke the REAL handler the guard installed exactly as the
    # kernel would (no mock: the actual registered callable).
    tripped = None
    # Act
    with std.standby_signal_guard():
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        tripped = std._should_stop()
    # Assert
    assert tripped is True


def test_signal_guard_restores_prior_sigterm_handler() -> None:
    # Arrange — capture the handler in force before the guard.
    prior = signal.getsignal(signal.SIGTERM)
    # Act
    with std.standby_signal_guard():
        pass
    # Assert — the guard restored it on exit.
    assert signal.getsignal(signal.SIGTERM) is prior


# ---------------------------------------------------------------------------
# _default_take_over — ASK the holder to leave; never destroy it
# ---------------------------------------------------------------------------


def test_take_over_gracefully_stops_the_tracked_holder(tmp_path: Path) -> None:
    # Arrange — a live tracked holder named by the pidfile.
    port = 7878
    (tmp_path / f"listen-{port}.pid").write_text("4242\n")
    graceful = _GracefulRecorder()
    # Act
    with (
        _swap("pid_alive", lambda _p: True),
        _swap("_terminate_graceful", graceful),
        _swap("_sleep", _no_sleep),
    ):
        std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert — a graceful SIGTERM against the holder PID.
    assert graceful.calls == [4242]


def test_take_over_keeps_the_pidfile_intact(tmp_path: Path) -> None:
    # Arrange — the take-over must NOT unlink the pidfile. The flock (not
    # the file's existence) is the bind arbiter, and unlinking it lets a
    # racing standby flock the OLD inode while a fresh acquirer creates a
    # NEW one — both would then "hold the lock" and both would bind.
    port = 7878
    pidfile = tmp_path / f"listen-{port}.pid"
    pidfile.write_text("4242\n")
    # Act
    with (
        _swap("pid_alive", lambda _p: True),
        _swap("_terminate_graceful", _GracefulRecorder()),
        _swap("_sleep", _no_sleep),
    ):
        std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert
    assert pidfile.exists()


def test_take_over_of_dead_holder_is_a_noop(tmp_path: Path) -> None:
    # Arrange — the holder is already gone; the kernel released its flock,
    # so there is nothing to stop and no error to report.
    port = 7878
    (tmp_path / f"listen-{port}.pid").write_text("4242\n")
    graceful = _GracefulRecorder()
    # Act
    with (
        _swap("pid_alive", lambda _p: False),
        _swap("_terminate_graceful", graceful),
        _swap("_sleep", _no_sleep),
    ):
        error = std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert — no signal sent, no error.
    assert error == "" and graceful.calls == []


def test_take_over_surfaces_sigterm_refusal(tmp_path: Path) -> None:
    # Arrange — the holder ignores SIGTERM. We do NOT escalate; we report
    # a loud, actionable failure and let the human decide.
    port = 7878
    (tmp_path / f"listen-{port}.pid").write_text("4242\n")
    # Act
    with (
        _swap("pid_alive", lambda _p: True),
        _swap("_terminate_graceful", _GracefulRecorder(died=False)),
        _swap("_sleep", _no_sleep),
    ):
        error = std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert
    assert "sac listen restart --force" in error


# ---------------------------------------------------------------------------
# _default_probe_health — REAL loopback socket, bounded, three-state
# ---------------------------------------------------------------------------


def test_probe_health_serving_for_live_loopback_server() -> None:
    # Arrange — a real HTTP server answering any GET (proves "up").
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    _host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Act
    try:
        probe = std._default_probe_health("127.0.0.1", port, timeout=1.0)
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert
    assert probe.health is HolderHealth.SERVING


def test_probe_health_unreachable_for_closed_port() -> None:
    # Arrange — port 1 on loopback refuses. "I asked and got nothing" is
    # its own state; it is emphatically NOT health.
    # Act
    probe = std._default_probe_health("127.0.0.1", 1, timeout=0.2)
    # Assert
    assert probe.health is HolderHealth.UNREACHABLE


def test_probe_health_closed_port_is_not_serving() -> None:
    # Arrange — the ``serving`` predicate is what the loop branches on.
    # Act
    probe = std._default_probe_health("127.0.0.1", 1, timeout=0.2)
    # Assert
    assert probe.serving is False
