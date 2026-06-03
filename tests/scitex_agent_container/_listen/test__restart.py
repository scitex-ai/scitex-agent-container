"""Tests for ``_listen/_restart.py`` — atomic stop-clean-relaunch.

PA-306 + STX-NM001-003 compliant: no MagicMock / no monkeypatch.
The four module-level injection points (``_kill``, ``_sleep``,
``_run_subprocess``, ``_http_get``) are swapped via a hand-rolled
save/restore context manager — same shape as
``test_image_group._use_apptainer``.

AAA + ≥3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scitex_agent_container._listen import _restart as restart_mod
from scitex_agent_container._listen._restart import (
    format_escalation_warning,
    pid_alive,
    pidfile_path,
    read_pid_from_file,
    restart_listen,
    systemd_unit_is_active,
    wait_for_health,
)

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


def _no_sleep(_secs: float) -> None:
    """Recorded fake: no-op sleep, makes the grace loop instant."""


class _KillRecorder:
    """Hand-rolled fake for ``os.kill`` — records every (pid, signal)
    call and lets the test program a script of liveness responses.

    ``script`` is consumed in order on each ``kill(pid, 0)`` probe:
    True/False decides whether the probe sees the pid alive. After
    the script is exhausted, the last value sticks.
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


class _SubprocessRecorder:
    """Hand-rolled fake for ``subprocess.run`` — records argv +
    returns a configurable rc / stdout / stderr per call.
    """

    def __init__(self, *, returncodes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._rcs = list(returncodes) if returncodes else []

    def __call__(self, *args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args", []))
        self.calls.append(argv)
        rc = self._rcs.pop(0) if self._rcs else 0
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout="", stderr=""
        )


class _HttpRecorder:
    """Hand-rolled fake for the http-get test seam — returns a
    scripted sequence of HTTP statuses.
    """

    def __init__(self, *, statuses: list[int]) -> None:
        self.calls: list[tuple[str, float]] = []
        self._statuses = list(statuses)

    def __call__(self, url: str, timeout: float) -> int:
        self.calls.append((url, timeout))
        if not self._statuses:
            return -1
        return self._statuses.pop(0)


# ---------------------------------------------------------------------------
# pidfile_path
# ---------------------------------------------------------------------------


def test_pidfile_path_is_listen_dash_port_dot_pid(tmp_path: Path) -> None:
    # Arrange
    lock_dir = tmp_path / "runtime"
    # Act
    p = pidfile_path(7878, lock_dir)
    # Assert
    assert p == lock_dir / "listen-7878.pid"


def test_pidfile_path_includes_port_for_isolation(tmp_path: Path) -> None:
    # Arrange — two different ports must produce different paths.
    lock_dir = tmp_path / "runtime"
    # Act
    p1 = pidfile_path(7878, lock_dir)
    p2 = pidfile_path(9999, lock_dir)
    # Assert
    assert p1 != p2


# ---------------------------------------------------------------------------
# read_pid_from_file
# ---------------------------------------------------------------------------


def test_read_pid_from_file_returns_int_for_clean_content(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "listen-7878.pid"
    f.write_text("12345\n")
    # Act
    pid = read_pid_from_file(f)
    # Assert
    assert pid == 12345


def test_read_pid_returns_none_for_missing_file(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "listen-7878.pid"
    # Act
    pid = read_pid_from_file(f)
    # Assert
    assert pid is None


def test_read_pid_returns_none_for_empty_file(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "listen-7878.pid"
    f.write_text("")
    # Act
    pid = read_pid_from_file(f)
    # Assert
    assert pid is None


def test_read_pid_returns_none_for_malformed_content(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "listen-7878.pid"
    f.write_text("not-a-number\n")
    # Act
    pid = read_pid_from_file(f)
    # Assert
    assert pid is None


# ---------------------------------------------------------------------------
# pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_returns_true_when_kill_zero_succeeds() -> None:
    # Arrange — a kill recorder that always reports the pid as alive.
    kill = _KillRecorder(alive_script=[True])
    # Act
    with _swap("_kill", kill):
        result = pid_alive(99999)
    # Assert
    assert result is True


def test_pid_alive_returns_false_when_kill_zero_raises_processlookup() -> None:
    # Arrange
    kill = _KillRecorder(alive_script=[False])
    # Act
    with _swap("_kill", kill):
        result = pid_alive(99999)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _terminate_then_kill (private but core; tested through restart_listen)
# ---------------------------------------------------------------------------


def test_force_flag_skips_term_and_uses_sigkill_immediately(tmp_path: Path) -> None:
    # Arrange — daemon hangs forever on TERM. force=True must skip
    # straight to SIGKILL.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[True, False])
    subproc = _SubprocessRecorder(returncodes=[0, 0])
    http = _HttpRecorder(statuses=[200])

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",  # no systemd
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — SIGKILL was sent (signal 9), no SIGTERM.
    signals_sent = [
        sig for _pid, sig in kill.calls if sig in (signal.SIGTERM, signal.SIGKILL)
    ]
    assert signals_sent == [signal.SIGKILL]


def test_term_clean_exit_does_not_escalate(tmp_path: Path) -> None:
    # Arrange — daemon dies on first poll after TERM. No SIGKILL.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    # alive_script: [True, True, False] — initial alive check + TERM + first poll dead
    kill = _KillRecorder(alive_script=[True, False])
    subproc = _SubprocessRecorder(returncodes=[0, 0])
    http = _HttpRecorder(statuses=[200])

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            grace_secs=1.0,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.escalated_to_sigkill is False


def test_term_hang_escalates_to_sigkill_after_grace(tmp_path: Path) -> None:
    # Arrange — daemon stays alive past the grace deadline → SIGKILL
    # escalation fires. alive_script always True for the poll loop.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[True, True, True, True, True, False, False])
    subproc = _SubprocessRecorder(returncodes=[0, 0])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            grace_secs=0.4,  # short grace → escalation
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.escalated_to_sigkill is True


# ---------------------------------------------------------------------------
# Pidfile cleanup
# ---------------------------------------------------------------------------


def test_restart_clears_stale_pidfile_after_dead_pid(tmp_path: Path) -> None:
    # Arrange — pid in file is already dead. Pidfile must be removed.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[False])  # pid is dead from the start
    subproc = _SubprocessRecorder(returncodes=[0, 0])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert pid_file.exists() is False


def test_restart_with_no_prior_pidfile_is_clean_noop(tmp_path: Path) -> None:
    # Arrange — no existing pidfile (fresh restart, nothing to stop).
    # Should still relaunch + health-check.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0, 0])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.had_prior_pidfile is False


# ---------------------------------------------------------------------------
# Relaunch path detection — systemd
# ---------------------------------------------------------------------------


def test_systemd_unit_is_active_returns_false_for_missing_file(tmp_path: Path) -> None:
    # Arrange
    absent = tmp_path / "sac-listen.service"
    # Act
    result = systemd_unit_is_active(absent)
    # Assert
    assert result is False


def test_systemd_unit_active_when_file_present_and_rc_zero(tmp_path: Path) -> None:
    # Arrange — file exists, systemctl returns rc=0 (enabled).
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")
    subproc = _SubprocessRecorder(returncodes=[0])
    # Act
    with _swap("_run_subprocess", subproc):
        result = systemd_unit_is_active(unit)
    # Assert
    assert result is True


def test_systemd_unit_inactive_when_file_present_but_rc_nonzero(tmp_path: Path) -> None:
    # Arrange — file copied in but never enabled.
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")
    subproc = _SubprocessRecorder(returncodes=[1])
    # Act
    with _swap("_run_subprocess", subproc):
        result = systemd_unit_is_active(unit)
    # Assert
    assert result is False


def test_systemd_path_calls_daemon_reload_before_restart(tmp_path: Path) -> None:
    # Arrange — unit installed AND enabled. Expect daemon-reload
    # FIRST then restart, in that order per design call (b).
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")
    pid_file = tmp_path / "listen-7878.pid"
    # is-enabled rc=0, daemon-reload rc=0, restart rc=0
    subproc = _SubprocessRecorder(returncodes=[0, 0, 0])
    kill = _KillRecorder(alive_script=[False])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=unit,
        )
    # Assert — daemon-reload preceded restart
    cmds = [" ".join(c) for c in subproc.calls]
    reload_idx = next(i for i, c in enumerate(cmds) if "daemon-reload" in c)
    restart_idx = next(
        i for i, c in enumerate(cmds) if "restart" in c and "sac-listen.service" in c
    )
    assert reload_idx < restart_idx


def test_systemd_path_marks_took_systemd_path_true(tmp_path: Path) -> None:
    # Arrange
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")
    subproc = _SubprocessRecorder(returncodes=[0, 0, 0])
    kill = _KillRecorder(alive_script=[False])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=unit,
        )
    # Assert
    assert result.took_systemd_path is True


def test_direct_spawn_path_when_systemd_unit_absent(tmp_path: Path) -> None:
    # Arrange — no systemd unit, fall back to direct spawn.
    subproc = _SubprocessRecorder(returncodes=[0])
    kill = _KillRecorder(alive_script=[False])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.took_systemd_path is False


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_wait_for_health_returns_true_on_first_200() -> None:
    # Arrange
    http = _HttpRecorder(statuses=[200])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        result = wait_for_health(host="127.0.0.1", port=7878, deadline_secs=5.0)
    # Assert
    assert result is True


def test_wait_for_health_returns_false_when_never_200() -> None:
    # Arrange
    http = _HttpRecorder(statuses=[503, -1, 502])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        result = wait_for_health(host="127.0.0.1", port=7878, deadline_secs=1.0)
    # Assert
    assert result is False


def test_wait_for_health_polls_correct_url() -> None:
    # Arrange
    http = _HttpRecorder(statuses=[200])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        wait_for_health(host="127.0.0.1", port=7878, deadline_secs=5.0)
    # Assert
    assert http.calls[0][0] == "http://127.0.0.1:7878/v1/sac/health"


# ---------------------------------------------------------------------------
# Full result shape
# ---------------------------------------------------------------------------


def test_successful_restart_returns_ok_true(tmp_path: Path) -> None:
    # Arrange
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.ok is True


def test_failed_health_check_sets_ok_false(tmp_path: Path) -> None:
    # Arrange — relaunch fires but the new daemon never responds 200.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[-1, -1])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            health_deadline_secs=0.5,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.ok is False


def test_failed_health_check_carries_error_message(tmp_path: Path) -> None:
    # Arrange
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[-1])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            health_deadline_secs=0.5,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert "/v1/sac/health" in result.error


# ---------------------------------------------------------------------------
# format_escalation_warning — exact wire string
# ---------------------------------------------------------------------------


def test_warning_names_the_grace_window_in_seconds() -> None:
    # Arrange
    # Act
    msg = format_escalation_warning(10.0)
    # Assert
    assert "10.0s" in msg


def test_warning_names_sigkill_explicitly() -> None:
    # Arrange
    # Act
    msg = format_escalation_warning(10.0)
    # Assert
    assert "SIGKILL" in msg


def test_warning_points_to_systemd_readme_for_manual_recovery() -> None:
    # Arrange
    # Act
    msg = format_escalation_warning(10.0)
    # Assert
    assert "scripts/systemd/README.md" in msg
