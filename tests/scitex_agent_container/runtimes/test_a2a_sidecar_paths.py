"""Per-agent sidecar path isolation, independent of native server startup."""

import functools
from pathlib import Path

import pytest

from scitex_agent_container.runtimes.a2a_sidecar import _log_path, _pid_path
from tests.scitex_agent_container.runtimes._a2a_sidecar_fixture import (
    _config,
    workdir as workdir,
)


def test_pid_path_differs_per_agent_in_one_shared_workdir(workdir: Path) -> None:
    # Arrange: two agents, ONE workdir — the clone / twin / relocate shape.
    cfg_a = _config(workdir, name="shared-wd-alpha")
    cfg_b = _config(workdir, name="shared-wd-bravo")
    # Act
    paths = {_pid_path(cfg_a), _pid_path(cfg_b)}
    # Assert
    assert len(paths) == 2


def test_log_path_differs_per_agent_in_one_shared_workdir(workdir: Path) -> None:
    # Arrange
    cfg_a = _config(workdir, name="shared-wd-alpha")
    cfg_b = _config(workdir, name="shared-wd-bravo")
    # Act
    paths = {_log_path(cfg_a), _log_path(cfg_b)}
    # Assert
    assert len(paths) == 2


def test_pid_path_is_keyed_by_the_agent_name_directory(workdir: Path) -> None:
    # Arrange: identity belongs in the DIRECTORY, like every other per-agent
    # runtime artefact (pid / heartbeat.json / session.jsonl / runner.log).
    cfg = _config(workdir, name="dir-keyed-agent")
    # Act
    parent_name = _pid_path(cfg).parent.name
    # Assert
    assert parent_name == "dir-keyed-agent"


def test_pid_path_refuses_an_unnamed_agent(workdir: Path) -> None:
    # Arrange: a silent fallback to a shared path here would reproduce the bug.
    cfg = _config(workdir, name="")
    # Act
    resolve = functools.partial(_pid_path, cfg)
    # Assert
    with pytest.raises(ValueError, match="empty name"):
        resolve()
