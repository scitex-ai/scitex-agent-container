"""Tests for ``_listen/_stop.py`` — the stop half ``stop`` and ``restart`` share.

``sac listen stop`` and ``sac listen restart`` must never drift: a restart
IS a stop plus a relaunch plus a health-probe. ``stop_listen`` is that
stop half as ONE implementation, so the two verbs cannot diverge. These
tests pin the sequence (SIGTERM → grace → SIGKILL escalation; verify-dead
BEFORE clearing the pidfile; wedged-port self-heal; idempotent no-op on a
dead daemon) AND that ``restart_listen`` still routes through it.

PA-306 + STX-NM001-003: no MagicMock / no monkeypatch. The seams
(``_kill``, ``_sleep``) are swapped on ``_restart`` via a hand-rolled
save/restore context manager, exactly as ``test__restart.py`` does —
``_stop`` reaches every primitive through the ``_restart`` MODULE OBJECT
at call time, so one swap drives both modules. A ``from ._restart import
_sleep`` in the production code would capture the ORIGINAL callable and
silently defeat these swaps; that is why it does not do that.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._listen import _port_holder as ph_mod
from scitex_agent_container._listen import _restart as restart_mod
from scitex_agent_container._listen import _stop as stop_mod
from scitex_agent_container._listen._stop import StopResult, stop_listen

# A port that is NOT the live control plane (7878). Belt-and-braces with
# the autouse seam fixture below: nothing here may ever reach the real
# fleet daemon.
TEST_PORT = 7999


# ---------------------------------------------------------------------------
# Module-level swap helpers — production callables → recording fakes.
# ---------------------------------------------------------------------------


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    """Replace ``restart_mod.<name>`` for the duration of the block."""
    saved = getattr(restart_mod, name)
    setattr(restart_mod, name, value)
    try:
        yield
    finally:
        setattr(restart_mod, name, saved)


@contextmanager
def _swap_ph(name: str, value) -> Iterator[None]:
    """Replace a ``_port_holder.<name>`` seam for the block.

    The discovery seams (``_probe_bound`` / ``_resolve_pids``) live on
    ``_port_holder``, not ``_restart``.
    """
    saved = getattr(ph_mod, name)
    setattr(ph_mod, name, value)
    try:
        yield
    finally:
        setattr(ph_mod, name, saved)


@pytest.fixture(autouse=True)
def _neutralize_port_seams() -> Iterator[None]:
    """Make the wedged-port-holder self-heal a hard no-op by default.

    SAFETY: the real ``port_is_bound`` does a live socket connect, and on a
    dev host the central ``sac listen`` is actually bound on 7878. Without
    this guard a ``stop_listen`` test could probe a real port, find it held,
    and FORCE-KILL the live fleet control plane as a test side effect.
    Defaulting the probe to "not bound" keeps every test hermetic; the
    dedicated self-heal test overrides these two seams inside its own block.
    """
    saved_bound = ph_mod._probe_bound
    saved_holders = ph_mod._resolve_pids
    ph_mod._probe_bound = lambda _host, _port: False
    ph_mod._resolve_pids = lambda _port: []
    try:
        yield
    finally:
        ph_mod._probe_bound = saved_bound
        ph_mod._resolve_pids = saved_holders


def _no_sleep(_secs: float) -> None:
    """Recorded fake: no-op sleep, makes the grace loop instant."""


class _KillRecorder:
    """Hand-rolled fake for ``os.kill`` — records every (pid, signal) call
    and lets the test program a script of liveness responses.

    ``alive_script`` is consumed in order on each ``kill(pid, 0)`` probe:
    True/False decides whether the probe sees the pid alive. After the
    script is exhausted, the last value sticks.
    """

    def __init__(self, *, alive_script: list[bool]) -> None:
        self.calls: list[tuple[int, int]] = []
        self._script = list(alive_script)
        self._last_alive = alive_script[-1] if alive_script else False

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if sig == 0:
            if self._script:
                alive = self._script.pop(0)
                self._last_alive = alive
            else:
                alive = self._last_alive
            if not alive:
                raise ProcessLookupError(pid)

    @property
    def signals(self) -> list[int]:
        """Every non-probe signal actually delivered."""
        return [sig for _pid, sig in self.calls if sig != 0]


def _write_pidfile(lock_dir: Path, pid: int, port: int = TEST_PORT) -> Path:
    """Create ``<lock_dir>/listen-<port>.pid`` holding ``pid``."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    pid_file = restart_mod.pidfile_path(port, lock_dir)
    pid_file.write_text(f"{pid}\n", encoding="utf-8")
    return pid_file


# ---------------------------------------------------------------------------
# stop_listen — the TERM → KILL ladder
# ---------------------------------------------------------------------------


def test_stop_listen_sends_sigterm_to_the_pidfile_pid(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    # alive for the pre-check, then dead on the first grace poll.
    kills = _KillRecorder(alive_script=[True, False, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert signal.SIGTERM in kills.signals


def test_stop_listen_force_skips_term_and_sends_sigkill(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(
            host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path, force=True
        )
    # Assert
    assert kills.signals == [signal.SIGKILL]


def test_stop_listen_clean_term_exit_does_not_escalate(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True, False, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.escalated_to_sigkill is False


def test_stop_listen_escalates_to_sigkill_after_grace(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    # Alive through every grace poll, then dead once SIGKILL lands.
    kills = _KillRecorder(alive_script=[True] * 60 + [False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(
            host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path, grace_secs=1.0
        )
    # Assert
    assert result.escalated_to_sigkill is True


# ---------------------------------------------------------------------------
# stop_listen — pidfile hygiene
# ---------------------------------------------------------------------------


def test_stop_listen_clears_the_pidfile_after_stopping(tmp_path: Path) -> None:
    # Arrange
    pid_file = _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True, False, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert pid_file.exists() is False


def test_stop_listen_clears_a_stale_pidfile_naming_a_dead_pid(
    tmp_path: Path,
) -> None:
    """A pidfile left behind by a crashed daemon must not wedge `stop`."""
    # Arrange
    pid_file = _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert pid_file.exists() is False


def test_stop_listen_keeps_pidfile_when_pid_survives_sigkill(
    tmp_path: Path,
) -> None:
    """Defence-in-depth: never clear a lock whose owner is still alive."""
    # Arrange
    pid_file = _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True])  # never dies
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(
            host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path, force=True
        )
    # Assert
    assert pid_file.exists() is True


def test_stop_listen_fails_when_pid_survives_sigkill(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True])  # never dies
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(
            host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path, force=True
        )
    # Assert
    assert result.ok is False


def test_stop_listen_surviving_pid_error_names_the_pid(tmp_path: Path) -> None:
    """FAIL LOUD: the error names the REAL cause, not a generic failure."""
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True])  # never dies
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(
            host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path, force=True
        )
    # Assert
    assert "4242" in result.error


# ---------------------------------------------------------------------------
# stop_listen — idempotence (the `systemctl stop` contract)
# ---------------------------------------------------------------------------


def test_stop_listen_with_no_pidfile_reports_ok(tmp_path: Path) -> None:
    """Stopping an already-stopped daemon is SUCCESS, not failure."""
    # Arrange
    kills = _KillRecorder(alive_script=[False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.ok is True


def test_stop_listen_with_no_pidfile_reports_not_running(tmp_path: Path) -> None:
    # Arrange
    kills = _KillRecorder(alive_script=[False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.was_running is False


def test_stop_listen_with_no_pidfile_sends_no_signals(tmp_path: Path) -> None:
    # Arrange
    kills = _KillRecorder(alive_script=[False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert kills.signals == []


def test_stop_listen_reports_was_running_for_a_live_daemon(
    tmp_path: Path,
) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True, False, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.was_running is True


def test_stop_listen_reports_the_pid_it_stopped(tmp_path: Path) -> None:
    # Arrange
    _write_pidfile(tmp_path, 4242)
    kills = _KillRecorder(alive_script=[True, False, False])
    # Act
    with _swap("_kill", kills), _swap("_sleep", _no_sleep):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.prior_pid == 4242


# ---------------------------------------------------------------------------
# stop_listen — wedged-port self-heal (the "curl hangs forever" remnant)
# ---------------------------------------------------------------------------


def test_stop_listen_force_kills_an_untracked_wedged_port_holder(
    tmp_path: Path,
) -> None:
    """The remnant the pidfile never named must still be cleared."""
    # Arrange — no pidfile at all; the port is held by an unknown PID.
    kills = _KillRecorder(alive_script=[True, False, False])
    bound = [True]  # bound on the first probe, freed after the kill

    def _probe(_host: str, _port: int) -> bool:
        return bound.pop(0) if bound else False

    # Act
    with (
        _swap("_kill", kills),
        _swap("_sleep", _no_sleep),
        _swap_ph("_probe_bound", _probe),
        _swap_ph("_resolve_pids", lambda _port: [9191]),
    ):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.port_holders_killed == (9191,)


def test_stop_listen_reports_was_running_for_a_wedged_port_holder(
    tmp_path: Path,
) -> None:
    """A wedged remnant with no pidfile still counts as 'was running'."""
    # Arrange
    kills = _KillRecorder(alive_script=[True, False, False])
    bound = [True]

    def _probe(_host: str, _port: int) -> bool:
        return bound.pop(0) if bound else False

    # Act
    with (
        _swap("_kill", kills),
        _swap("_sleep", _no_sleep),
        _swap_ph("_probe_bound", _probe),
        _swap_ph("_resolve_pids", lambda _port: [9191]),
    ):
        result = stop_listen(host="127.0.0.1", port=TEST_PORT, lock_dir=tmp_path)
    # Assert
    assert result.was_running is True


# ---------------------------------------------------------------------------
# SSOT — restart MUST NOT re-implement the stop sequence.
# ---------------------------------------------------------------------------


def test_restart_listen_delegates_its_stop_half_to_stop_listen(
    tmp_path: Path,
) -> None:
    """The anti-drift guard: one stop sequence, two verbs."""
    # Arrange
    calls: list[dict] = []

    def _recording_stop(**kwargs) -> StopResult:
        calls.append(kwargs)
        return StopResult(
            ok=True,
            escalated_to_sigkill=False,
            had_prior_pidfile=False,
            prior_pid_alive=False,
        )

    saved = stop_mod.stop_listen
    stop_mod.stop_listen = _recording_stop
    # Act
    try:
        with (
            _swap("_sleep", _no_sleep),
            _swap("_run_subprocess", lambda *_a, **_kw: None),
            _swap("_http_get", lambda _url, timeout: 200),
        ):
            restart_mod.restart_listen(
                host="127.0.0.1",
                port=TEST_PORT,
                lock_dir=tmp_path,
                systemd_unit_path=tmp_path / "absent.service",
                sac_listen_argv=["/bin/true"],
            )
    finally:
        stop_mod.stop_listen = saved
    # Assert
    assert len(calls) == 1


def test_restart_listen_forwards_force_to_the_stop_half(tmp_path: Path) -> None:
    # Arrange
    calls: list[dict] = []

    def _recording_stop(**kwargs) -> StopResult:
        calls.append(kwargs)
        return StopResult(
            ok=True,
            escalated_to_sigkill=False,
            had_prior_pidfile=False,
            prior_pid_alive=False,
        )

    saved = stop_mod.stop_listen
    stop_mod.stop_listen = _recording_stop
    # Act
    try:
        with (
            _swap("_sleep", _no_sleep),
            _swap("_run_subprocess", lambda *_a, **_kw: None),
            _swap("_http_get", lambda _url, timeout: 200),
        ):
            restart_mod.restart_listen(
                host="127.0.0.1",
                port=TEST_PORT,
                lock_dir=tmp_path,
                force=True,
                systemd_unit_path=tmp_path / "absent.service",
                sac_listen_argv=["/bin/true"],
            )
    finally:
        stop_mod.stop_listen = saved
    # Assert
    assert calls[0]["force"] is True


def test_restart_listen_aborts_when_the_stop_half_fails(tmp_path: Path) -> None:
    """A failed stop must never relaunch into a still-held port."""
    # Arrange
    def _failing_stop(**_kwargs) -> StopResult:
        return StopResult(
            ok=False,
            escalated_to_sigkill=True,
            had_prior_pidfile=True,
            prior_pid_alive=True,
            prior_pid=4242,
            error="PID 4242 survived SIGKILL",
        )

    saved = stop_mod.stop_listen
    stop_mod.stop_listen = _failing_stop
    spawns: list[list[str]] = []
    # Act
    try:
        with (
            _swap("_sleep", _no_sleep),
            _swap("_run_subprocess", lambda *a, **_kw: spawns.append(list(a[0]))),
            _swap("_http_get", lambda _url, timeout: 200),
        ):
            restart_mod.restart_listen(
                host="127.0.0.1",
                port=TEST_PORT,
                lock_dir=tmp_path,
                systemd_unit_path=tmp_path / "absent.service",
                sac_listen_argv=["/bin/true"],
            )
    finally:
        stop_mod.stop_listen = saved
    # Assert
    assert spawns == []
