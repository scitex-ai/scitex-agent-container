"""Tests for the restart's stop-leg escalation — no mocks, real processes.

The agent that survives SIGTERM is a REAL OS child process that REALLY
installs ``signal.SIG_IGN`` for SIGTERM, so the operator's bug shape
("previous runtime still running after 15.00s — SIGTERM ignored") is
REPRODUCED rather than simulated. The escalation then sends a REAL
SIGKILL through the REAL ``os.kill``, and the test asserts on the REAL
child's exit status (``-signal.SIGKILL``). Nothing about the kill path is
faked — a fake here would prove nothing, since the entire question is
whether sac kills the right process for real.

The runtimes are hand-rolled real classes implementing the production
``RuntimeBase`` surface (the pidless one subclasses ``RuntimeBase`` so its
``agent_pid`` IS the production default, not a test re-implementation).

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._stop_escalate import (
    StopEscalationError,
    ensure_previous_runtime_down,
    escalate_to_sigkill,
    long_lived_pid,
)
from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.runtimes.base import RuntimeBase

# A child that IGNORES SIGTERM — the exact behaviour that made the stop leg
# give up. It announces readiness on stdout so the test never races the
# handler installation (a SIGTERM delivered before ``SIG_IGN`` is armed
# would kill it and silently invalidate the whole test).
_SIGTERM_DEAF = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "sys.stdout.write('armed\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


def _no_sleep(_seconds: float) -> None:
    return None


def _write_spec(tmp_path: Path, *, name: str = "alpha", runtime: str = "tui") -> Path:
    """A real, validator-passing v3 spec at ``<tmp>/<name>/spec.yaml``.

    ``load_config`` derives the agent name from the parent directory
    (dir-as-SSoT), so the file must live at ``<name>/spec.yaml``.
    """
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        f"  runtime: {runtime}\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
        "  hooks:\n"
        "    pre_start: []\n"
        "    post_start: []\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
    )
    return spec


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path) -> Iterator[None]:
    """Redirect HOME so nothing here can read or write the LIVE fleet state.

    Production code resolves ``~/.scitex/agent-container/...`` via
    ``Path.home()``. Without this, the suite would read the developer's real
    registry — passing locally and failing in CI, where no fleet exists.
    """
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev


@pytest.fixture
def config(tmp_path: Path) -> AgentConfig:
    return load_config(str(_write_spec(tmp_path)))


@pytest.fixture
def deaf_proc() -> Iterator[subprocess.Popen]:
    """A REAL child process that ignores SIGTERM. Always reaped."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGTERM_DEAF],
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.stdout is not None:
        proc.stdout.readline()  # blocks until SIG_IGN is armed
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


class _RealProcessRuntime(RuntimeBase):
    """A runtime whose agent IS a real OS process.

    ``stop`` sends a REAL SIGTERM (which the deaf child ignores),
    ``is_running`` reads the REAL process state, and ``agent_pid`` reports
    the REAL long-lived pid — the seam the escalation aims SIGKILL at.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self.stop_calls = 0
        self.start_calls = 0

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.start_calls += 1
        return True

    def stop(self, config: AgentConfig) -> bool:
        self.stop_calls += 1
        self._proc.terminate()  # a REAL SIGTERM
        return True

    def is_running(self, config: AgentConfig) -> bool:
        return self._proc.poll() is None

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        return ""

    def agent_pid(self, config: AgentConfig) -> int | None:
        return self._proc.pid


class _PidlessRuntime(RuntimeBase):
    """Alive, and cannot name a local pid — the docker / SSHRemote shape.

    ``agent_pid`` is NOT overridden: this exercises the REAL
    ``RuntimeBase.agent_pid`` default (``None`` = honestly unknown), which
    is precisely the case where escalation is impossible.
    """

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.start_calls += 1
        return True

    def stop(self, config: AgentConfig) -> bool:
        return False

    def is_running(self, config: AgentConfig) -> bool:
        return True

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        return ""


class _UnkillableRuntime(RuntimeBase):
    """Names a pid, but nothing makes it stop (SIGKILL wedged in D-state).

    ``kill_fn`` is injected by the caller as a real recording callable, so
    no real process is signalled and the "SIGKILL landed but the process is
    STILL there" branch is reachable deterministically.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self.signals: list[int] = []

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        return True

    def stop(self, config: AgentConfig) -> bool:
        return False

    def is_running(self, config: AgentConfig) -> bool:
        return True

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        return ""

    def agent_pid(self, config: AgentConfig) -> int | None:
        return self._pid


class _StoppedRuntime(RuntimeBase):
    """Already down — the healthy teardown."""

    def __init__(self) -> None:
        self.pid_reads = 0

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        return True

    def stop(self, config: AgentConfig) -> bool:
        return True

    def is_running(self, config: AgentConfig) -> bool:
        return False

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        return ""

    def agent_pid(self, config: AgentConfig) -> int | None:
        self.pid_reads += 1
        return 1234


# ---------------------------------------------------------------------------
# long_lived_pid — the seam that decides WHAT gets SIGKILLed
# ---------------------------------------------------------------------------


def test_long_lived_pid_reports_the_real_process_pid(
    config: AgentConfig, deaf_proc: subprocess.Popen
) -> None:
    # Arrange
    runtime = _RealProcessRuntime(deaf_proc)
    # Act
    pid = long_lived_pid(runtime, config)
    # Assert
    assert pid == deaf_proc.pid


def test_long_lived_pid_is_none_without_the_seam(config: AgentConfig) -> None:
    # Arrange — the production RuntimeBase default (docker / SSHRemote).
    runtime = _PidlessRuntime()
    # Act
    pid = long_lived_pid(runtime, config)
    # Assert — honestly unknown, never a guess.
    assert pid is None


def test_long_lived_pid_refuses_pid_zero(config: AgentConfig) -> None:
    # Arrange — os.kill(0, SIGKILL) would signal sac's ENTIRE process group;
    # a runtime bug returning 0 must never become a fleet-wide massacre.
    runtime = _UnkillableRuntime(pid=0)
    # Act
    pid = long_lived_pid(runtime, config)
    # Assert
    assert pid is None


def test_long_lived_pid_refuses_negative_pid(config: AgentConfig) -> None:
    # Arrange — os.kill(-1, ...) signals every process this user owns.
    runtime = _UnkillableRuntime(pid=-1)
    # Act
    pid = long_lived_pid(runtime, config)
    # Assert
    assert pid is None


def test_long_lived_pid_refuses_our_own_pid(config: AgentConfig) -> None:
    # Arrange — sac must not SIGKILL itself mid-restart.
    runtime = _UnkillableRuntime(pid=os.getpid())
    # Act
    pid = long_lived_pid(runtime, config)
    # Assert
    assert pid is None


# ---------------------------------------------------------------------------
# escalate_to_sigkill — a REAL SIGKILL against a REAL SIGTERM-deaf process
# ---------------------------------------------------------------------------


def test_escalation_really_kills_the_sigterm_deaf_process(
    config: AgentConfig, deaf_proc: subprocess.Popen
) -> None:
    # Arrange — the real bug shape: SIGTERM is sent and IGNORED.
    runtime = _RealProcessRuntime(deaf_proc)
    runtime.stop(config)
    # Act — real os.kill, no injected kill_fn.
    escalate_to_sigkill(config.name, config, runtime, sleep_fn=_no_sleep)
    # Assert — the REAL child died of a REAL SIGKILL.
    assert deaf_proc.wait(timeout=10) == -signal.SIGKILL


def test_escalation_reports_stopped_after_the_kill(
    config: AgentConfig, deaf_proc: subprocess.Popen
) -> None:
    # Arrange
    runtime = _RealProcessRuntime(deaf_proc)
    runtime.stop(config)
    # Act
    stopped, _pid = escalate_to_sigkill(
        config.name, config, runtime, sleep_fn=_no_sleep
    )
    # Assert — the verdict is the runtime's OWN is_running, post-kill.
    assert stopped is True


def test_escalation_reports_the_pid_it_killed(
    config: AgentConfig, deaf_proc: subprocess.Popen
) -> None:
    # Arrange
    runtime = _RealProcessRuntime(deaf_proc)
    # Act
    _stopped, pid = escalate_to_sigkill(
        config.name, config, runtime, sleep_fn=_no_sleep
    )
    # Assert
    assert pid == deaf_proc.pid


def test_escalation_cannot_kill_without_a_pid(config: AgentConfig) -> None:
    # Arrange — a runtime that is UP and cannot name a pid.
    runtime = _PidlessRuntime()
    # Act
    stopped, _pid = escalate_to_sigkill(
        config.name, config, runtime, sleep_fn=_no_sleep
    )
    # Assert — no signal was sent and the agent is still up: NOT stopped.
    assert stopped is False


def test_escalation_sends_sigkill_not_sigterm(config: AgentConfig) -> None:
    # Arrange — a real recording kill callable (injection seam, not a mock).
    runtime = _UnkillableRuntime(pid=999_000)
    # Act
    escalate_to_sigkill(
        config.name,
        config,
        runtime,
        sleep_fn=_no_sleep,
        settle_s=0.01,
        kill_fn=lambda pid, sig: runtime.signals.append(sig),
    )
    # Assert — the escalation is a KILL; the TERM already failed.
    assert runtime.signals == [signal.SIGKILL]


def test_escalation_reports_not_stopped_when_kill_does_not_land(
    config: AgentConfig,
) -> None:
    # Arrange — SIGKILL "delivered" but the process is still there
    # (uninterruptible D-state I/O).
    runtime = _UnkillableRuntime(pid=999_000)
    # Act
    stopped, _pid = escalate_to_sigkill(
        config.name,
        config,
        runtime,
        sleep_fn=_no_sleep,
        settle_s=0.01,
        kill_fn=lambda pid, sig: runtime.signals.append(sig),
    )
    # Assert — we sent the signal, but we do NOT claim it worked.
    assert stopped is False


# ---------------------------------------------------------------------------
# ensure_previous_runtime_down — the gate: escalate, else RAISE (never proceed)
# ---------------------------------------------------------------------------


def _gate(
    tmp_path: Path,
    runtime: Any,
    *,
    timeout_s: float = 0.05,
    settle_s: float = 5.0,
    kill_fn: Any = os.kill,
    name: str = "alpha",
) -> None:
    """Act helper: run the REAL gate against ``runtime``.

    ``timeout_s`` (the SIGTERM grace) is tiny so the test is fast. ``settle_s``
    keeps the PRODUCTION default: SIGKILLing a REAL process and seeing it reaped
    is not instantaneous, and a sub-100ms settle here would flake on a loaded
    host — which would be the test lying about a kill that did in fact work.
    Tests that assert the RAISE path pass a small ``settle_s`` explicitly,
    because in those the process is deliberately never going to die.
    """
    ensure_previous_runtime_down(
        name,
        str(tmp_path / name / "spec.yaml"),
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        timeout_s=timeout_s,
        settle_s=settle_s,
        kill_fn=kill_fn,
    )


def test_gate_returns_silently_when_runtime_already_stopped(tmp_path: Path) -> None:
    # Arrange
    _write_spec(tmp_path)
    runtime = _StoppedRuntime()
    # Act
    _gate(tmp_path, runtime)
    # Assert — a healthy teardown never reaches the kill path.
    assert runtime.pid_reads == 0


def test_gate_escalates_and_kills_the_deaf_runtime(
    tmp_path: Path, deaf_proc: subprocess.Popen
) -> None:
    # Arrange — SIGTERM sent and ignored; the grace will expire.
    _write_spec(tmp_path)
    runtime = _RealProcessRuntime(deaf_proc)
    runtime.stop(load_config(str(tmp_path / "alpha" / "spec.yaml")))
    # Act
    _gate(tmp_path, runtime)
    # Assert — the survivor is REALLY dead, so the start leg cannot collide.
    assert deaf_proc.wait(timeout=10) == -signal.SIGKILL


def test_gate_raises_when_runtime_cannot_be_stopped(tmp_path: Path) -> None:
    # Arrange — up, and impossible to kill (no nameable pid).
    _write_spec(tmp_path)
    # Act
    call = lambda: _gate(tmp_path, _PidlessRuntime())  # noqa: E731
    # Assert — the gate REFUSES to fall through into a guaranteed collision.
    with pytest.raises(StopEscalationError):
        call()


def test_gate_error_says_the_agent_was_not_restarted(tmp_path: Path) -> None:
    # Arrange
    _write_spec(tmp_path)
    message = ""
    # Act
    try:
        _gate(tmp_path, _PidlessRuntime())
    except StopEscalationError as exc:
        message = str(exc)
    # Assert — the old code printed "restarted" here; the message must say
    # the opposite, in the operator's own terms.
    assert "was NOT restarted" in message


def test_gate_error_names_the_still_running_agent(tmp_path: Path) -> None:
    # Arrange
    _write_spec(tmp_path, name="neurovista")
    message = ""
    # Act
    try:
        _gate(tmp_path, _PidlessRuntime(), name="neurovista")
    except StopEscalationError as exc:
        message = str(exc)
    # Assert
    assert "neurovista" in message


def test_gate_error_carries_a_remedy_that_works(tmp_path: Path) -> None:
    # Arrange — a TUI agent (the neurovista shape).
    _write_spec(tmp_path, name="neurovista", runtime="tui")
    message = ""
    # Act
    try:
        _gate(tmp_path, _PidlessRuntime(), name="neurovista")
    except StopEscalationError as exc:
        message = str(exc)
    # Assert — NOT `sac agents restart` (the command that just failed).
    assert "tmux kill-session -t tui-neurovista" in message


def test_gate_error_carries_the_pid_it_could_not_kill(tmp_path: Path) -> None:
    # Arrange
    _write_spec(tmp_path)
    runtime = _UnkillableRuntime(pid=999_000)
    captured: object = None
    # Act
    try:
        _gate(
            tmp_path,
            runtime,
            settle_s=0.05,
            kill_fn=lambda pid, sig: runtime.signals.append(sig),
        )
    except StopEscalationError as exc:
        captured = exc.pid
    # Assert — structured, so a caller need not re-parse the message.
    assert captured == 999_000


def test_gate_bypasses_entirely_on_zero_timeout(tmp_path: Path) -> None:
    # Arrange — the legacy fixed-sleep bypass, retained for callers that
    # deliberately skip the gate. A running runtime must NOT raise here.
    _write_spec(tmp_path)
    runtime = _PidlessRuntime()
    # Act
    _gate(tmp_path, runtime, timeout_s=0.0)
    # Assert — reaching this line at all is the contract (no raise).
    assert runtime.is_running(load_config(str(tmp_path / "alpha" / "spec.yaml")))


# ---------------------------------------------------------------------------
# agent_restart — the whole chain, end to end.
#
# The operator's terminal, 2026-07-14:
#
#   WARN: previous runtime still running after 15.00s (SIGTERM ignored...);
#         proceeding to start anyway.
#   FAIL: duplicate session 'tui-neurovista' — agent already running.
#   Agent 'neurovista' restarted        <-- IT WAS NOT
#
# Two contracts, one per failure mode of the stop leg:
#   * the survivor CAN be killed  -> kill it, then really restart;
#   * the survivor CANNOT be killed -> RAISE. Never start (the start would
#     collide with it), never report success.
# ---------------------------------------------------------------------------


class _FakeHandover:
    """Real collaborator matching the handover module surface."""

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "fake-uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def push_pre_stop_snapshot(
        self, config: AgentConfig, payload: dict | None = None
    ) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        return None


def _restart(tmp_path: Path, runtime: Any, *, name: str = "alpha") -> bool:
    """Act helper: drive the REAL ``agent_restart`` against ``runtime``."""
    from scitex_agent_container._lifecycle import lifecycle as lc
    from scitex_agent_container._state.registry import Registry

    registry = Registry(registry_dir=tmp_path / "reg")
    registry.add(name, str(tmp_path / name / "spec.yaml"), f"cld-{name}")
    return lc.agent_restart(
        name,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=_FakeHandover(),
        # The credential pre-flight is a separate production seam with its own
        # suite; injecting a real no-op callable keeps THIS test about the stop
        # leg (and off the network).
        successor_auth_check=lambda _path: None,
        wait_for_stop_timeout_s=0.05,
    )


def test_restart_kills_the_runtime_that_ignored_sigterm(
    tmp_path: Path, deaf_proc: subprocess.Popen
) -> None:
    # Arrange — the neurovista shape: a REAL process that ignores SIGTERM.
    _write_spec(tmp_path)
    # Act
    _restart(tmp_path, _RealProcessRuntime(deaf_proc))
    # Assert — the previous runtime is REALLY gone, so the new one cannot
    # collide with it ("duplicate session") on the way up.
    assert deaf_proc.wait(timeout=10) == -signal.SIGKILL


def test_restart_starts_the_replacement_after_escalating(
    tmp_path: Path, deaf_proc: subprocess.Popen
) -> None:
    # Arrange
    _write_spec(tmp_path)
    runtime = _RealProcessRuntime(deaf_proc)
    # Act
    _restart(tmp_path, runtime)
    # Assert — escalation is not an abort: the restart still restarts.
    assert runtime.start_calls == 1


def test_restart_raises_when_the_survivor_cannot_be_killed(tmp_path: Path) -> None:
    # Arrange — up, and unkillable (no nameable pid).
    _write_spec(tmp_path)
    # Act
    call = lambda: _restart(tmp_path, _PidlessRuntime())  # noqa: E731
    # Assert — the old code returned True here and the CLI printed
    # "Agent 'neurovista' restarted" over an agent that was left DOWN.
    with pytest.raises(StopEscalationError):
        call()


def test_restart_never_starts_over_a_surviving_runtime(tmp_path: Path) -> None:
    # Arrange
    _write_spec(tmp_path)
    runtime = _PidlessRuntime()
    # Act
    try:
        _restart(tmp_path, runtime)
    except StopEscalationError:
        pass
    # Assert — the gate must not walk into the collision it just predicted.
    assert runtime.start_calls == 0
