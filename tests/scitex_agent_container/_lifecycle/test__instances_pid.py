"""``instances.pid`` is populated with the agent's LONG-LIVED pid.

ROOT CAUSE (fleet a2a outage): ``record_instance_start`` accepts ``pid=``
but NO caller ever passed it — measured on a real host registry, 0 of 1229
rows had ever carried a pid. So the registry could never PROVE an agent
alive, and every pid consumer degraded:

  * ``_listen._liveness_tick._live_agent_pids`` gates on
    ``isinstance(pid, int)`` -> dropped every agent -> ``resolve_liveness``
    reported every card owner DEAD (the stuck-card alarm misfired on live
    agents).
  * ``_state.state_db_gc.gc_dead_instances`` pid-liveness heuristic and
    ``_lifecycle._stale_lease.clear_stale_instance_lease`` both skip a NULL
    pid -> both were permanent no-ops (0 ``crashed`` / ``stale-cleared``
    rows in 1229).
  * ``cli_pkg._send_diagnosis`` -> ``pid_alive=None`` ("unknowable") for
    every agent, so ``agent_send``'s own diagnosis could never confirm a
    live target.

THE NAIVE FIX IS WRONG: the LAUNCHER pid is not the agent. For a TUI agent
the launcher spawns a tmux session and exits within seconds. The correct
long-lived pid is the one each runtime's own ``is_running`` already probes:
the tmux PANE pid for TUI (the pane's ``bash -c`` ``exec``s apptainer, and
``exec`` KEEPS the pid, so the pane pid IS the container process), and the
``<state_dir>/apptainer_pid`` process for the SDK/apptainer runtime.

Real on-disk SQLite state.db (env-overridden per test), real pid files, real
OS pids (``os.getpid()`` is by definition alive; a reaped child pid is by
definition dead). No mocks.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env (explicit save/restore)."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


class _PidRuntime:
    """Honest runtime collaborator implementing the ``RuntimeBase`` seam.

    Mirrors the real runtimes' shape: a ``_state_dir`` resolver plus the
    ``agent_pid`` accessor. ``start`` re-reads ``pid_source`` so a restart
    can hand back a DIFFERENT pid, exactly as a respawned process would.
    """

    def __init__(self, root: Path, pid: int | None) -> None:
        self._root = root
        self.pid = pid

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name

    def agent_pid(self, config: AgentConfig) -> int | None:
        del config
        return self.pid

    def start(self, config: AgentConfig) -> bool:
        del config
        return True


class _LegacyRuntime:
    """A runtime predating the ``agent_pid`` seam (back-compat guard)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _row_for(name: str) -> dict:
    from scitex_agent_container._state.state_db import list_active_instances

    return [r for r in list_active_instances() if r["name"] == name][0]


def _dead_pid() -> int:
    """A pid that is genuinely dead: run a trivial child and reap it.

    Uses ``subprocess`` rather than ``os.fork()`` on purpose — pytest runs
    multi-threaded, and forking a multi-threaded process risks deadlocking
    the child (CPython raises DeprecationWarning for exactly this). The
    child is waited on, so its pid is reaped and provably not alive.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# record_local_instance now persists the runtime's long-lived pid
# ---------------------------------------------------------------------------


def test_record_local_instance_persists_runtime_pid(db_path, tmp_path) -> None:
    # Arrange — the runtime reports the live pid of this very process.
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="pid-1", runtime="apptainer")
    # Act
    record_local_instance(cfg, _PidRuntime(tmp_path, os.getpid()))
    # Assert
    assert _row_for("pid-1")["pid"] == os.getpid()


def test_record_local_instance_leaves_pid_null_for_legacy_runtime(
    db_path, tmp_path
) -> None:
    # Arrange — a runtime without the seam must not fabricate a pid.
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="pid-legacy", runtime="apptainer")
    # Act
    record_local_instance(cfg, _LegacyRuntime(tmp_path))
    # Assert
    assert _row_for("pid-legacy")["pid"] is None


def test_runtime_pid_rejects_nonpositive_pid(tmp_path) -> None:
    # Arrange — a 0/negative pid is not a process; it must degrade to None.
    from scitex_agent_container._lifecycle._instances import _runtime_pid

    cfg = AgentConfig(name="pid-zero", runtime="apptainer")
    # Act
    resolved = _runtime_pid(cfg, _PidRuntime(tmp_path, 0))
    # Assert
    assert resolved is None


def test_runtime_pid_rejects_bool_pid(tmp_path) -> None:
    # Arrange — bool is an int subclass; True must not become pid 1.
    from scitex_agent_container._lifecycle._instances import _runtime_pid

    cfg = AgentConfig(name="pid-bool", runtime="apptainer")
    rt = _PidRuntime(tmp_path, None)
    rt.pid = True  # type: ignore[assignment]
    # Act
    resolved = _runtime_pid(cfg, rt)
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# restart must REFRESH the pid — a stale pid is worse than no pid
# ---------------------------------------------------------------------------


def test_restart_and_record_refreshes_stale_pid(db_path, tmp_path) -> None:
    # Arrange — agent recorded with a now-dead pid, then the supervisor
    # restarts it as a NEW process (the health-monitor's callback path).
    from scitex_agent_container._lifecycle._instances import (
        record_local_instance,
        restart_and_record,
    )

    cfg = AgentConfig(name="pid-restart", runtime="apptainer")
    rt = _PidRuntime(tmp_path, _dead_pid())
    record_local_instance(cfg, rt)
    rt.pid = os.getpid()  # the restarted process
    # Act
    restart_and_record(cfg, lambda _c: rt)
    # Assert — the row now points at the LIVE process, not the dead one.
    assert _row_for("pid-restart")["pid"] == os.getpid()


def test_restart_and_record_returns_runtime_verdict(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import restart_and_record

    cfg = AgentConfig(name="pid-restart-rc", runtime="apptainer")
    # Act
    started = restart_and_record(cfg, lambda _c: _PidRuntime(tmp_path, os.getpid()))
    # Assert
    assert started is True


# ---------------------------------------------------------------------------
# END-TO-END: a live agent resolves LIVE through agent_send's own path
# (the scitex-hpc failure mode: process up, but the registry could not
#  prove it, so agent_send would not vouch for the target)
# ---------------------------------------------------------------------------


def test_live_agent_resolves_pid_alive_through_send_diagnosis(
    db_path, tmp_path
) -> None:
    # Arrange — start a LIVE agent (pid = this process) exactly as
    # agent_start does, with a bound sidecar port.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state import port_allocator
    from scitex_agent_container._state.state_db import _resolve_host
    from scitex_agent_container.cli_pkg._send_diagnosis import diagnose_send_failure

    port_allocator.claim_port("pid-live", explicit=7931)
    record_local_instance(
        AgentConfig(name="pid-live", runtime="apptainer"),
        _PidRuntime(tmp_path, os.getpid()),
    )
    host = _resolve_host(None)
    # Act — the SAME diagnosis agent_send runs before it dispatches.
    diagnosis = diagnose_send_failure(
        "pid-live", a2a_port=7931, peer_host=host, current_host=host
    )
    # Assert — was None ("unknowable") for every agent before this fix.
    assert diagnosis["pid_alive"] is True


def test_dead_agent_still_resolves_pid_not_alive(db_path, tmp_path) -> None:
    # Arrange — a genuinely dead process must still be reported dead, so the
    # fix cannot mask a crash by over-claiming liveness.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state import port_allocator
    from scitex_agent_container._state.state_db import _resolve_host
    from scitex_agent_container.cli_pkg._send_diagnosis import diagnose_send_failure

    port_allocator.claim_port("pid-dead", explicit=7932)
    record_local_instance(
        AgentConfig(name="pid-dead", runtime="apptainer"),
        _PidRuntime(tmp_path, _dead_pid()),
    )
    host = _resolve_host(None)
    # Act
    diagnosis = diagnose_send_failure(
        "pid-dead", a2a_port=7932, peer_host=host, current_host=host
    )
    # Assert
    assert diagnosis["pid_alive"] is False


def test_live_agent_resolves_live_through_liveness_tick(db_path, tmp_path) -> None:
    # Arrange — the stuck-card alarm's owner-liveness resolver read EVERY
    # agent as dead while pids were NULL (_live_agent_pids dropped them all).
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._listen._liveness_tick import resolve_liveness

    record_local_instance(
        AgentConfig(name="pid-owner", runtime="apptainer"),
        _PidRuntime(tmp_path, os.getpid()),
    )
    # Act
    liveness = resolve_liveness(["pid-owner"])
    # Assert
    assert liveness["pid-owner"].is_live is True


# ---------------------------------------------------------------------------
# The GC's pid-liveness heuristic can finally fire (it was a no-op on NULL)
# ---------------------------------------------------------------------------


def test_gc_reaps_row_whose_recorded_pid_is_dead(db_path, tmp_path) -> None:
    # Arrange — before the fix this heuristic never fired: with pid NULL,
    # gc_dead_instances skipped every row (0 'crashed' rows in 1229).
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db_gc import gc_dead_instances

    record_local_instance(
        AgentConfig(name="pid-gc", runtime="apptainer"),
        _PidRuntime(tmp_path, _dead_pid()),
    )
    # Act
    counters = gc_dead_instances()
    # Assert
    assert counters["crashed"] == 1


def test_gc_spares_row_whose_recorded_pid_is_alive(db_path, tmp_path) -> None:
    # Arrange — the reaper must never sweep a LIVE agent.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances
    from scitex_agent_container._state.state_db_gc import gc_dead_instances

    record_local_instance(
        AgentConfig(name="pid-gc-live", runtime="apptainer"),
        _PidRuntime(tmp_path, os.getpid()),
    )
    gc_dead_instances()
    # Act
    names = [r["name"] for r in list_active_instances()]
    # Assert
    assert "pid-gc-live" in names


@pytest.mark.skipif(
    os.getuid() == 0, reason="as root, os.kill(1, 0) does not raise PermissionError"
)
def test_gc_spares_live_process_owned_by_another_uid(db_path, tmp_path) -> None:
    # Arrange — pid 1 is ALIVE but not ours: os.kill(1, 0) raises
    # PermissionError, a subclass of OSError. The GC caught OSError broadly
    # and would have reaped this LIVE row as 'crashed' — ending the row is
    # precisely what makes send_to_agent report "agent not running". The
    # branch was dormant while pids were NULL; recording pids activates it.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db_gc import gc_dead_instances

    record_local_instance(
        AgentConfig(name="pid-gc-foreign", runtime="apptainer"),
        _PidRuntime(tmp_path, 1),
    )
    # Act
    counters = gc_dead_instances()
    # Assert — a live foreign-uid process is proof of life, never a crash.
    assert counters["crashed"] == 0
