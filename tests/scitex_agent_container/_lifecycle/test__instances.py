"""Local ``instances`` row bookkeeping (sac-node-comms fix).

A LOCAL ``sac agent start`` previously created no state.db ``instances``
row, so ``send_to_agent`` reported "agent not running" and ``/v1/turn``
was unreachable. ``record_local_instance`` / ``end_local_instance`` close
that gap. Tests use a real on-disk SQLite state.db (isolated per test via
the ``SCITEX_AGENT_CONTAINER_STATE_DB`` env override) and a real runtime
stub exposing ``_state_dir`` — no mocks.
"""

from __future__ import annotations

import importlib
import os
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


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver
    that ``_instances`` calls. Mirrors ApptainerContainerRuntime's API."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _active_names(host: str | None = None) -> list[str]:
    from scitex_agent_container._state.state_db import (
        _resolve_host,
        list_active_instances,
    )

    rows = list_active_instances(host=host or _resolve_host(None))
    return [r["name"] for r in rows]


def _claim_port(name: str, port: int) -> None:
    from scitex_agent_container._state import port_allocator

    port_allocator.claim_port(name, explicit=port)


# ---------------------------------------------------------------------------
# record_local_instance
# ---------------------------------------------------------------------------


def test_record_local_instance_creates_active_row(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="rec-1", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    # Act
    record_local_instance(cfg, rt)
    # Assert
    assert "rec-1" in _active_names()


def test_record_local_instance_persists_resolved_a2a_port(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-2", runtime="apptainer")
    _claim_port("rec-2", 7901)
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "rec-2"][0]
    assert row["a2a_port"] == 7901


def test_record_local_instance_mirrors_bound_port(db_path, tmp_path) -> None:
    # Arrange — local row's bound_port mirrors the allocator-claimed port.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-bp", runtime="apptainer")
    _claim_port("rec-bp", 7902)
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "rec-bp"][0]
    assert row["bound_port"] == 7902


def test_record_local_instance_marks_remote_false(db_path, tmp_path) -> None:
    # Arrange — a local start records remote=0 (it ran on THIS host).
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-loc", runtime="apptainer")
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "rec-loc"][0]
    assert row["remote"] == 0


def test_record_local_instance_records_cli_spawned_by_without_sac_name(
    db_path, tmp_path
) -> None:
    # Arrange — no SAC_NAME in env → launcher is the bare CLI/lead.
    saved = os.environ.pop("SAC_NAME", None)
    saved_long = os.environ.pop("SCITEX_AGENT_CONTAINER_NAME", None)
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-cli", runtime="apptainer")
    try:
        # Act
        record_local_instance(cfg, _RuntimeStub(tmp_path))
    finally:
        if saved is not None:
            os.environ["SAC_NAME"] = saved
        if saved_long is not None:
            os.environ["SCITEX_AGENT_CONTAINER_NAME"] = saved_long
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "rec-cli"][0]
    assert row["spawned_by"] == "cli"


def test_record_local_instance_records_parent_spawned_by_from_sac_name(
    db_path, tmp_path
) -> None:
    # Arrange — a parent agent shelled out; SAC_NAME carries its name.
    saved = os.environ.get("SAC_NAME")
    os.environ["SAC_NAME"] = "parent-bot"
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-child", runtime="apptainer")
    try:
        # Act
        record_local_instance(cfg, _RuntimeStub(tmp_path))
    finally:
        if saved is None:
            os.environ.pop("SAC_NAME", None)
        else:
            os.environ["SAC_NAME"] = saved
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "rec-child"][0]
    assert row["spawned_by"] == "parent-bot"


def test_record_local_instance_writes_instance_id_marker(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="rec-3", runtime="apptainer")
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert (tmp_path / "rec-3" / "instance_id").is_file()


def test_record_local_instance_supersedes_stale_active_row(db_path, tmp_path) -> None:
    # Arrange — two records for the same name; the unique partial index
    # would reject the second unless the first is ended first.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db import list_active_instances

    cfg = AgentConfig(name="rec-4", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    # Act
    record_local_instance(cfg, rt)
    # Assert — exactly one active row remains for the name.
    active = [r for r in list_active_instances() if r["name"] == "rec-4"]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# end_local_instance
# ---------------------------------------------------------------------------


def test_end_local_instance_clears_active_row(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import (
        end_local_instance,
        record_local_instance,
    )

    cfg = AgentConfig(name="end-1", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    # Act
    end_local_instance(cfg, rt)
    # Assert
    assert "end-1" not in _active_names()


def test_end_local_instance_returns_true_when_row_ended(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import (
        end_local_instance,
        record_local_instance,
    )

    cfg = AgentConfig(name="end-2", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    # Act
    result = end_local_instance(cfg, rt)
    # Assert
    assert result is True


def test_end_local_instance_removes_instance_id_marker(db_path, tmp_path) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import (
        end_local_instance,
        record_local_instance,
    )

    cfg = AgentConfig(name="end-3", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    # Act
    end_local_instance(cfg, rt)
    # Assert
    assert not (tmp_path / "end-3" / "instance_id").is_file()
