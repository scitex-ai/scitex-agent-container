"""Each runtime's ``agent_pid`` returns its OWN long-lived pid.

The ``RuntimeBase.agent_pid`` seam is what hands ``instances.pid`` its
value (``_lifecycle._instances.record_local_instance``). The contract is
that every runtime returns the SAME pid its own ``is_running`` probes with
``os.kill(pid, 0)`` — so the registry and ``is_running`` can never disagree
about which process represents an agent:

  * TUI  -> the tmux PANE pid. The pane's ``bash -c`` ``exec``s apptainer
    and ``exec`` KEEPS the pid, so the pane pid IS the long-lived
    ``apptainer exec ... claude`` process. NOT the launcher, which spawns
    the session and exits within seconds.
  * SDK / apptainer -> the ``apptainer`` process pid persisted by
    ``ApptainerRuntime.start`` to ``<state_dir>/apptainer_pid``.
  * Anything that cannot name a long-lived local pid -> ``None``, honestly
    "unknown". A wrong pid is worse than none: pids are REUSED, so a stale
    one can be recycled by an unrelated process and vouch for a dead agent.

Real pid files on disk and a REAL tmux session (skipped when tmux is
unavailable). No mocks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._tui_liveness import pane_pid_of


# ---------------------------------------------------------------------------
# TUI runtime -> tmux pane pid (the long-lived in-pane process)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmux_session():
    """A REAL detached tmux session running a long-lived process."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    name = f"sac-test-panepid-{int(time.time() * 1000)}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "sleep 300"],
        check=True,
        capture_output=True,
    )
    try:
        yield name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_tui_agent_pid_is_the_live_pane_process(tmux_session) -> None:
    # Arrange — the pane's process is a real, live `sleep`.
    from scitex_agent_container._runners._tmux.tmux import TmuxManager

    # Act
    pid = pane_pid_of(tmux_session, pane_pid_fn=TmuxManager.pane_pid)
    # Assert — a concrete, live OS pid (not the launcher, which already exited).
    assert pid is not None and pid > 0


def test_tui_agent_pid_matches_is_running_signal(tmux_session) -> None:
    # Arrange — the registry pid and the liveness verdict must agree.
    from scitex_agent_container._runners._tmux.tmux import TmuxManager
    from scitex_agent_container.runtimes._tui_liveness import pid_alive

    # Act
    pid = pane_pid_of(tmux_session, pane_pid_fn=TmuxManager.pane_pid)
    # Assert — the pid recorded in instances.pid is provably alive.
    assert pid_alive(pid) is True


def test_tui_agent_pid_is_none_for_absent_session() -> None:
    # Arrange
    from scitex_agent_container._runners._tmux.tmux import TmuxManager

    # Act
    pid = pane_pid_of("sac-test-no-such-session", pane_pid_fn=TmuxManager.pane_pid)
    # Assert
    assert pid is None


def test_pane_pid_of_is_none_without_a_probe() -> None:
    # Arrange — a multiplexer fake predating the probe must yield "unknown",
    # never a fabricated pid.
    # Act
    pid = pane_pid_of("whatever", pane_pid_fn=None)
    # Assert
    assert pid is None


# ---------------------------------------------------------------------------
# Apptainer / SDK runtime -> the persisted apptainer process pid
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path):
    """Redirect ``$HOME`` so the runtime's state dir lands under tmp_path.

    ``ApptainerContainerRuntime._state_dir`` resolves to
    ``~/.scitex/agent-container/runtime/<name>/`` for a home-scope agent.
    Pointing ``$HOME`` at tmp_path keeps the real home clean while the
    runtime still runs its REAL resolver against a REAL directory — the
    same explicit save/restore env pattern the state.db fixtures use.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_apptainer_agent_pid_reads_the_persisted_pid_file(isolated_home: Path) -> None:
    # Arrange — a real apptainer_pid file, exactly as
    # ApptainerContainerRuntime.start writes it after Popen.
    from scitex_agent_container.runtimes._apptainer_runtime import (
        APPTAINER_PID_FILE,
        ApptainerContainerRuntime,
    )

    cfg = AgentConfig(name="pidfile-agent", runtime="apptainer")
    rt = ApptainerContainerRuntime()
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / APPTAINER_PID_FILE).write_text("4242")
    # Act
    pid = rt.agent_pid(cfg)
    # Assert
    assert pid == 4242


def test_apptainer_agent_pid_is_none_without_pid_file(isolated_home: Path) -> None:
    # Arrange — no pid file => the runtime has no pid to offer.
    from scitex_agent_container.runtimes._apptainer_runtime import (
        ApptainerContainerRuntime,
    )

    cfg = AgentConfig(name="pidfile-absent", runtime="apptainer")
    # Act
    pid = ApptainerContainerRuntime().agent_pid(cfg)
    # Assert
    assert pid is None


# ---------------------------------------------------------------------------
# Base seam default: honest "unknown", never a fabricated pid
# ---------------------------------------------------------------------------


def test_runtime_base_agent_pid_defaults_to_none() -> None:
    # Arrange — a runtime that cannot name a long-lived local pid (docker /
    # podman / SSHRemote) inherits the honest None.
    from scitex_agent_container.runtimes.base import RuntimeBase

    cfg = AgentConfig(name="base-default", runtime="apptainer")
    # Act
    pid = RuntimeBase.agent_pid(object(), cfg)  # type: ignore[arg-type]
    # Assert
    assert pid is None
