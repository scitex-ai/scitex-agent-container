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

import pytest

from scitex_agent_container._listen import _port_holder as ph_mod
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


@contextmanager
def _swap_ph(name: str, value) -> Iterator[None]:
    """Replace a ``_port_holder.<name>`` seam for the block. The
    discovery seams (``_probe_bound`` / ``_resolve_pids``) live on
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

    SAFETY: the real ``port_is_bound`` does a live socket connect, and
    on a dev host the central ``sac listen`` is actually bound on 7878.
    Without this guard a ``restart_listen(port=7878)`` test would probe
    the real port, find it held, and FORCE-KILL the live fleet listen
    as a test side effect. Defaulting the probe to "not bound" keeps
    every test hermetic; the dedicated self-heal tests override these
    two seams inside their own ``with`` block.
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


def test_wait_for_health_returns_false_when_transport_fails() -> None:
    # Arrange — every probe is a transport failure (refused/timeout),
    # i.e. ``-1``: the only signal that means the daemon is truly down.
    http = _HttpRecorder(statuses=[-1, -1, -1])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        result = wait_for_health(host="127.0.0.1", port=7878, deadline_secs=1.0)
    # Assert
    assert result is False


def test_wait_for_health_returns_true_on_401_under_bearer_auth() -> None:
    # Arrange — bearer-auth gate answers the unauthenticated probe with
    # 401: the daemon is ALIVE (it answered), so liveness must pass.
    http = _HttpRecorder(statuses=[401])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        result = wait_for_health(host="127.0.0.1", port=7878, deadline_secs=5.0)
    # Assert
    assert result is True


def test_wait_for_health_polls_correct_url() -> None:
    # Arrange
    http = _HttpRecorder(statuses=[200])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        wait_for_health(host="127.0.0.1", port=7878, deadline_secs=5.0)
    # Assert — the ONLY registered liveness route is /v1/health, NOT
    # the previously-probed /v1/sac/health (latent-bug fix, card
    # sac-listen-restart-selfheal-cli).
    assert http.calls[0][0] == "http://127.0.0.1:7878/v1/health"


def test_wait_for_health_does_not_poll_v1_sac_health() -> None:
    # Arrange — guard against regressing back to the unregistered route.
    http = _HttpRecorder(statuses=[200])
    # Act
    with _swap("_http_get", http), _swap("_sleep", _no_sleep):
        wait_for_health(host="127.0.0.1", port=7878, deadline_secs=5.0)
    # Assert
    assert "/v1/sac/health" not in http.calls[0][0]


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
    # Arrange — health never answers AND the port is not bound (default
    # neutralized seam) → the "bind failed" fail-loud branch fires.
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
    # Assert — loud, names the real cause (bind failed) + the right route.
    assert "ERROR: bind failed" in result.error and "/v1/health" in result.error


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


# ---------------------------------------------------------------------------
# Uncovered error-branch coverage
# ---------------------------------------------------------------------------


def test_systemd_unit_is_active_returns_false_when_systemctl_missing(
    tmp_path: Path,
) -> None:
    # Arrange — unit file present but systemctl binary missing (or
    # subprocess raises FileNotFoundError on a non-systemd host).
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")

    def _missing_systemctl(*args, **kwargs):
        raise FileNotFoundError("systemctl: command not found")

    # Act
    with _swap("_run_subprocess", _missing_systemctl):
        result = systemd_unit_is_active(unit)
    # Assert
    assert result is False


def test_default_http_get_returns_minus_one_for_unreachable_url() -> None:
    # Arrange — the real default callable (no swap) against a port
    # nothing is bound on. Returns -1 per the URLError → -1 contract.
    from scitex_agent_container._listen._restart import _default_http_get

    # Act
    status = _default_http_get("http://127.0.0.1:1/never-bound", timeout=0.1)
    # Assert
    assert status == -1


def test_term_skipped_when_pid_dies_between_check_and_kill(tmp_path: Path) -> None:
    # Arrange — pid is alive at the initial check, but ProcessLookupError
    # is raised when SIGTERM is sent (race: process died between the
    # liveness probe and the signal). Result must NOT escalate, since
    # the process is already gone.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")

    class _DyingKill:
        """Pid is alive on the very first `kill(0)`, but ProcessLookupError
        on the SIGTERM (race) → no escalation."""

        def __init__(self):
            self.calls: list[tuple[int, int]] = []
            self._first_probe = True

        def __call__(self, pid: int, sig: int) -> None:
            self.calls.append((pid, sig))
            if sig == 0 and self._first_probe:
                self._first_probe = False
                return  # alive
            raise ProcessLookupError(pid)

    kill = _DyingKill()
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
    assert result.escalated_to_sigkill is False


def test_direct_spawn_filenotfound_reports_loud_error(tmp_path: Path) -> None:
    # Arrange — sac binary missing on $PATH; spawn must fail loudly
    # with the actionable error in ``result.error``.
    pid_file = tmp_path / "listen-7878.pid"
    kill = _KillRecorder(alive_script=[False])

    def _missing_sac(*args, **kwargs):
        raise FileNotFoundError("sac: command not found")

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", _missing_sac),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["sac", "listen"],
        )
    # Assert
    assert "direct spawn failed" in result.error


def test_systemctl_restart_nonzero_rc_reports_loud_error(tmp_path: Path) -> None:
    # Arrange — unit installed + enabled, but ``systemctl restart``
    # exits non-zero. Must surface in ``result.error`` rather than
    # silently claiming success.
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")
    # is-enabled rc=0, daemon-reload rc=0, restart rc=1
    subproc = _SubprocessRecorder(returncodes=[0, 0, 1])
    kill = _KillRecorder(alive_script=[False])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=unit,
        )
    # Assert
    assert "systemctl restart exited rc=1" in result.error


def test_systemctl_timeoutexpired_reports_loud_error(tmp_path: Path) -> None:
    # Arrange — systemctl hangs past the call's timeout.
    unit = tmp_path / "sac-listen.service"
    unit.write_text("[Unit]\n")

    # is-enabled returns 0 (alive). Then daemon-reload raises
    # TimeoutExpired. _SubprocessRecorder doesn't directly support
    # raising; build a stateful callable.
    state = {"n": 0}

    def _stateful_subproc(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            # is-enabled
            return subprocess.CompletedProcess(args=list(args[0]), returncode=0)
        # daemon-reload (or restart) — hang
        raise subprocess.TimeoutExpired(cmd=list(args[0]), timeout=10.0)

    kill = _KillRecorder(alive_script=[False])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", _stateful_subproc),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=unit,
        )
    # Assert
    assert "systemctl restart failed" in result.error


def test_post_sigkill_survival_refuses_to_clear_pidfile(tmp_path: Path) -> None:
    # Arrange — pid stays alive even after SIGKILL (zombie /
    # uninterruptible state). Must NOT clear the pidfile, must
    # surface the actionable error.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    # alive_script: always True — survives SIGTERM, SIGKILL, and the
    # defence-in-depth post-kill check.
    kill = _KillRecorder(alive_script=[True])
    # Act
    with _swap("_kill", kill), _swap("_sleep", _no_sleep):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            grace_secs=0.4,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert "survived SIGKILL" in result.error


# ---------------------------------------------------------------------------
# CLI verb (sac listen restart) — exercises the click integration
# ---------------------------------------------------------------------------


def test_cli_listen_restart_surface_invokes_restart_listen(tmp_path: Path) -> None:
    # Arrange — CliRunner against the listen group + restart subcommand.
    # The verb wires `host:port` from --bind into the restart_listen
    # call. We swap the restart_listen function on the module the CLI
    # imports it from to record + control the result.
    from click.testing import CliRunner

    from scitex_agent_container._listen import _restart as restart_mod_alias
    from scitex_agent_container.cli_pkg.listen_cmds import listen as listen_grp

    captured: dict[str, tuple] = {}

    def _fake_restart(**kwargs):
        captured["kwargs"] = kwargs
        from scitex_agent_container._listen._restart import RestartResult

        return RestartResult(
            ok=True,
            escalated_to_sigkill=False,
            had_prior_pidfile=False,
            prior_pid_alive=False,
            health_ok=True,
            took_systemd_path=False,
            error="",
        )

    runner = CliRunner()
    saved = restart_mod_alias.restart_listen
    restart_mod_alias.restart_listen = _fake_restart  # type: ignore[assignment]
    try:
        # Act
        result = runner.invoke(listen_grp, ["restart"])
    finally:
        restart_mod_alias.restart_listen = saved  # type: ignore[assignment]
    # Assert
    assert result.exit_code == 0 and "kwargs" in captured


def test_cli_listen_restart_surfaces_loud_warn_on_escalation(tmp_path: Path) -> None:
    # Arrange — restart_listen returns escalated=True; CLI must emit
    # the canonical WARN line to stderr per design call (c).
    from click.testing import CliRunner

    from scitex_agent_container._listen import _restart as restart_mod_alias
    from scitex_agent_container.cli_pkg.listen_cmds import listen as listen_grp

    def _fake_restart(**kwargs):
        from scitex_agent_container._listen._restart import RestartResult

        return RestartResult(
            ok=True,
            escalated_to_sigkill=True,
            had_prior_pidfile=True,
            prior_pid_alive=True,
            health_ok=True,
            took_systemd_path=False,
            error="",
        )

    runner = CliRunner()
    saved = restart_mod_alias.restart_listen
    restart_mod_alias.restart_listen = _fake_restart  # type: ignore[assignment]
    try:
        # Act
        result = runner.invoke(listen_grp, ["restart"])
    finally:
        restart_mod_alias.restart_listen = saved  # type: ignore[assignment]
    # Assert
    assert "WARN: escalated to SIGKILL" in result.output


def test_cli_listen_restart_failure_exits_nonzero_with_error(tmp_path: Path) -> None:
    # Arrange — restart_listen returns ok=False + populated error;
    # CLI must exit non-zero and surface the error.
    from click.testing import CliRunner

    from scitex_agent_container._listen import _restart as restart_mod_alias
    from scitex_agent_container.cli_pkg.listen_cmds import listen as listen_grp

    def _fake_restart(**kwargs):
        from scitex_agent_container._listen._restart import RestartResult

        return RestartResult(
            ok=False,
            escalated_to_sigkill=False,
            had_prior_pidfile=False,
            prior_pid_alive=False,
            health_ok=False,
            took_systemd_path=False,
            error="ERROR: bind failed — nothing is listening on 127.0.0.1:7878",
        )

    runner = CliRunner()
    saved = restart_mod_alias.restart_listen
    restart_mod_alias.restart_listen = _fake_restart  # type: ignore[assignment]
    try:
        # Act
        result = runner.invoke(listen_grp, ["restart"])
    finally:
        restart_mod_alias.restart_listen = saved  # type: ignore[assignment]
    # Assert — non-zero exit + the loud ERROR cause surfaces to stderr.
    assert result.exit_code != 0 and "ERROR: bind failed" in result.output


# ---------------------------------------------------------------------------
# Self-heal: wedged port holder ("curl hangs forever") force-killed
# (card sac-listen-restart-selfheal-cli). The pidfile is gone / names a
# dead PID, but an UNtracked remnant still holds the port. restart must
# discover + kill it, then start — no manual rm/pkill/setsid.
# ---------------------------------------------------------------------------


def test_restart_force_kills_untracked_port_holder(tmp_path: Path) -> None:
    # Arrange — NO pidfile (operator already rm-ed it), but the port is
    # still bound by a remnant PID 4242. force=True → SIGKILL it.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])
    # Port is bound BEFORE the kill, free AFTER (two probes per heal call).
    bound_states = iter([True, False])

    def _bound(_host: str, _port: int) -> bool:
        return next(bound_states, False)

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", _bound),
        _swap_ph("_resolve_pids", lambda _port: [4242]),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — the remnant PID was force-killed off the port.
    assert result.port_holders_killed == (4242,)


def test_restart_with_wedged_holder_sends_sigkill_to_remnant(tmp_path: Path) -> None:
    # Arrange — wedged remnant on the port; force=True must SIGKILL it
    # (the "curl hangs forever" recovery, no manual pkill).
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])
    bound_states = iter([True, False])

    def _bound(_host: str, _port: int) -> bool:
        return next(bound_states, False)

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", _bound),
        _swap_ph("_resolve_pids", lambda _port: [4242]),
    ):
        restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — SIGKILL (9) was delivered to the remnant PID 4242.
    assert (4242, signal.SIGKILL) in kill.calls


def test_restart_succeeds_after_clearing_wedged_holder(tmp_path: Path) -> None:
    # Arrange — remnant killed, port freed, relaunch answers health.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])
    bound_states = iter([True, False])

    def _bound(_host: str, _port: int) -> bool:
        return next(bound_states, False)

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", _bound),
        _swap_ph("_resolve_pids", lambda _port: [4242]),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.ok is True


def test_restart_fails_loud_when_holder_unkillable(tmp_path: Path) -> None:
    # Arrange — port stays bound even AFTER the force-kill (holder is a
    # different uid / zombie). Must fail loud naming the surviving PID,
    # NOT relaunch into EADDRINUSE.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", lambda _host, _port: True),  # never frees
        _swap_ph("_resolve_pids", lambda _port: [4242]),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — loud, non-empty error naming the surviving PID + port.
    assert result.ok is False and "still held by PID 4242" in result.error


def test_restart_fails_loud_when_port_held_but_no_pid_found(tmp_path: Path) -> None:
    # Arrange — port is bound but NO holder PID resolves (no
    # lsof/ss/fuser). Must NOT relaunch into EADDRINUSE; fail loud.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", lambda _host, _port: True),
        _swap_ph("_resolve_pids", lambda _port: []),  # nothing resolves
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            force=True,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.ok is False and "no holding PID could be resolved" in result.error


def test_restart_does_not_kill_when_port_free(tmp_path: Path) -> None:
    # Arrange — nothing holds the port (clean restart). The self-heal
    # must be a no-op: no holders recorded as killed.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[200])

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", lambda _host, _port: False),
        _swap_ph("_resolve_pids", lambda _port: [9999]),  # would-be, unused
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.port_holders_killed == ()


def test_unhealthy_with_bound_port_names_wedged_pid(tmp_path: Path) -> None:
    # Arrange — relaunch happens, port comes up bound, but health never
    # answers (up-but-not-serving). The fail-loud cause must name the
    # wedged PID, aligning with _lifecycle/_bind_watchdog.py.
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[-1, -1])
    # First two probes (the heal step) say not-bound so we relaunch;
    # the post-relaunch diagnosis probe says bound (wedged).
    bound_states = iter([False, True, True])

    def _bound(_host: str, _port: int) -> bool:
        return next(bound_states, True)

    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
        _swap_ph("_probe_bound", _bound),
        _swap_ph("_resolve_pids", lambda _port: [7171]),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            health_deadline_secs=0.5,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — loud "UP but NOT SERVING" cause naming the wedged PID.
    assert "NOT SERVING" in result.error and "7171" in result.error
