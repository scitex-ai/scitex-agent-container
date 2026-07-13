"""Tests for ``_listen/_standby.py`` — hot-standby + failover startup
(card ``sac-listen-hot-standby-no-crashloop``).

The bug: a second ``sac listen`` launched while one already holds the
port used to exit 1, which under systemd ``Restart=always`` was an
infinite CRASH-LOOP. ``resolve_startup`` turns the second instance into
a warm STANDBY that fails over instead of crashing.

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
from scitex_agent_container._listen._port_holder import PortHealResult
from scitex_agent_container._listen._single_instance import (
    ListenAlreadyRunningError,
    LockHandle,
)


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


class _TermRecorder:
    """Records (pid, force_kill) for each terminate call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, pid: int, *, grace_secs: float, force_kill: bool) -> bool:
        self.calls.append((pid, force_kill))
        return force_kill


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
# resolve_startup — healthy holder → STAND BY, then serve when freed
# ---------------------------------------------------------------------------


def test_healthy_holder_stands_by_then_serves_when_freed() -> None:
    # Arrange — contended once behind a HEALTHY holder, then it frees.
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=1, handle=sentinel)
    logs: list[str] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: True),
        _swap("_heal_untracked_port", lambda **_k: ""),
        _swap("_read_pid", lambda _p: 4242),
        _swap("_sleep", _no_sleep),
        _swap("_log", logs.append),
    ):
        result = std.resolve_startup(
            host="127.0.0.1",
            port=7878,
            lock_dir=Path("/tmp"),
            standby_interval=0.01,
        )
    # Assert — served after standing by behind the healthy holder.
    assert result is sentinel and any("standing by behind healthy" in m for m in logs)


def test_healthy_holder_never_takes_over() -> None:
    # Arrange — a healthy holder must NEVER be killed. Contended once,
    # then frees; the take-over seam must remain untouched.
    sentinel = _sentinel_handle()
    acquire = _FakeAcquire(raises=1, handle=sentinel)
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: True),
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
# resolve_startup — wedged (unhealthy) holder → TAKE OVER after corroboration
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
        _swap("_probe_health", lambda *_a, **_k: False),
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
    health = iter([False, True])
    takeovers: list[dict] = []
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: next(health, True)),
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


def test_stop_signal_during_standby_exits_without_binding() -> None:
    # Arrange — perpetually contended behind a healthy holder; a stop is
    # flagged on the first standby wait. resolve_startup must return None
    # (the caller then exits WITHOUT binding) rather than loop forever.
    acquire = _FakeAcquire(raises=999, handle=_sentinel_handle())
    # Act
    with (
        _swap("_acquire", acquire),
        _swap("_probe_health", lambda *_a, **_k: True),
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
    # Assert — clean standby exit signalled by None.
    assert result is None


# ---------------------------------------------------------------------------
# resolve_startup — unkillable port remnant on the serve path → stand by
# ---------------------------------------------------------------------------


def test_unkillable_port_remnant_releases_lock_and_stands_by() -> None:
    # Arrange — the flock acquires but an untracked remnant holds the port
    # and cannot be freed. Rather than crash-loop into EADDRINUSE, the
    # loop must RELEASE the flock and stand by (here we then stop).
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
# _default_take_over — kill holder + clear port + drop stale pidfile
# ---------------------------------------------------------------------------


def test_take_over_terminates_the_tracked_holder_pid(tmp_path: Path) -> None:
    # Arrange — a live tracked holder named by the pidfile.
    port = 7878
    (tmp_path / f"listen-{port}.pid").write_text("4242\n")
    term = _TermRecorder()
    # Act
    with (
        _swap("pid_alive", lambda _p: True),
        _swap("_terminate", term),
        _swap("_clear_port", lambda **_k: PortHealResult()),
        _swap("_sleep", _no_sleep),
    ):
        std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert — SIGTERM-first escalation against the holder PID.
    assert term.calls == [(4242, False)]


def test_take_over_removes_the_stale_pidfile(tmp_path: Path) -> None:
    # Arrange — pidfile present; take-over must drop it.
    port = 7878
    pidfile = tmp_path / f"listen-{port}.pid"
    pidfile.write_text("4242\n")
    # Act
    with (
        _swap("pid_alive", lambda _p: False),
        _swap("_terminate", _TermRecorder()),
        _swap("_clear_port", lambda **_k: PortHealResult()),
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
    assert not pidfile.exists()


def test_take_over_surfaces_unkillable_port_error(tmp_path: Path) -> None:
    # Arrange — the port stays held after the kill (unkillable remnant).
    port = 7878
    (tmp_path / f"listen-{port}.pid").write_text("4242\n")
    # Act
    with (
        _swap("pid_alive", lambda _p: False),
        _swap("_terminate", _TermRecorder()),
        _swap("_clear_port", lambda **_k: PortHealResult(error="still held by PID 9")),
        _swap("_sleep", _no_sleep),
    ):
        error = std._default_take_over(
            host="127.0.0.1",
            port=port,
            lock_dir=tmp_path,
            holder_pid=4242,
            grace_secs=1.0,
        )
    # Assert — the loud port error is surfaced to the caller.
    assert "still held by PID 9" in error


# ---------------------------------------------------------------------------
# _default_probe_health — REAL loopback socket, bounded
# ---------------------------------------------------------------------------


def test_probe_health_true_for_live_loopback_server() -> None:
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
        healthy = std._default_probe_health("127.0.0.1", port, timeout=1.0)
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert
    assert healthy is True


def test_probe_health_false_for_closed_port() -> None:
    # Arrange — port 1 on loopback refuses (transport failure == down).
    # Act
    healthy = std._default_probe_health("127.0.0.1", 1, timeout=0.2)
    # Assert
    assert healthy is False
