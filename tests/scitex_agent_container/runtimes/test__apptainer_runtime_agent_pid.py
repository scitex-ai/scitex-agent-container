"""``ApptainerContainerRuntime.agent_pid`` — the SDK runtime's recorded pid.

The ``RuntimeBase.agent_pid`` seam hands ``instances.pid`` its value
(``_lifecycle._instances.record_local_instance``). For the SDK / apptainer
runtime that value is the ``apptainer`` process pid that ``start`` persisted
to ``<state_dir>/apptainer_pid`` — the container process itself, launched
with ``start_new_session=True``, so it lives for the whole session.

It is deliberately the SAME pid this runtime's own ``is_running`` probes
with ``os.kill(pid, 0)`` (both go through ``_read_pid``), so the registry
and ``is_running`` cannot disagree about which process IS the agent.

Real pid files on disk, real state-dir resolution. No mocks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig


@pytest.fixture
def isolated_home(tmp_path: Path):
    """Redirect ``$HOME`` so the runtime's state dir lands under tmp_path.

    ``ApptainerContainerRuntime._state_dir`` resolves to
    ``~/.scitex/agent-container/runtime/<name>/`` for a home-scope agent.
    Pointing ``$HOME`` at tmp_path keeps the real home clean while the
    runtime still runs its REAL resolver against a REAL directory — the same
    explicit save/restore env pattern the state.db fixtures use.
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


def test_agent_pid_reads_the_persisted_apptainer_pid_file(isolated_home: Path) -> None:
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


def test_agent_pid_is_none_without_a_pid_file(isolated_home: Path) -> None:
    # Arrange — no pid file => the runtime has no pid to offer, and must say
    # "unknown" rather than invent one.
    from scitex_agent_container.runtimes._apptainer_runtime import (
        ApptainerContainerRuntime,
    )

    cfg = AgentConfig(name="pidfile-absent", runtime="apptainer")
    # Act
    pid = ApptainerContainerRuntime().agent_pid(cfg)
    # Assert
    assert pid is None
