"""ADR-0014 — listen-startup registers the operator identity.

The hook lives in :mod:`scitex_agent_container.cli_pkg.listen_cmds`
(``_register_self_comms_node`` + ``_maybe_sync_on_start``); this test
exercises both helpers directly without spinning a real uvicorn (the
``sac listen`` entry point flows through the same helpers, but binding
a real port + running the ASGI server is out of scope for a unit test).

Real on-disk state.db + config.yaml; no mocks.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def db_path(tmp_path: Path, env_save_restore):
    p = tmp_path / "state.db"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(p))
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    yield p
    importlib.reload(mod)


@pytest.fixture
def cfg_with_lead(tmp_path: Path, env_save_restore) -> Path:
    """config.yaml with a lead: block configured."""
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "host": {"canonical": "lead-host"},
                "lead": {"name": "lead", "host": "lead-host", "a2a_port": 8642},
                "peers": {},
            }
        )
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


@pytest.fixture
def cfg_no_lead(tmp_path: Path, env_save_restore) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"host": {"canonical": "h"}, "peers": {}}))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def test_register_self_writes_comms_nodes_row_for_lead_identity(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=8642)
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info is not None and info["host"] == "lead-host"


def test_register_self_records_correct_port(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=9000)
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["a2a_port"] == 9000


def test_register_self_no_lead_block_is_silent_noop(
    db_path: Path, cfg_no_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    # Act
    _register_self_comms_node(port=8642)
    # Assert — no row was written.
    assert list_comms_nodes() == []


def test_register_self_source_host_is_none(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=8642)
    # Assert — locally-registered rows have source_host = None.
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["source_host"] is None


def test_register_self_does_not_raise_when_config_missing(
    db_path: Path, tmp_path: Path, env_save_restore
) -> None:
    # Arrange — point config to a non-existent file. ``host_config.load``
    # is missing-tolerant so the hook should land in the "no lead"
    # silent-noop branch, never raise.
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "absent.yaml")
    )
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act + Assert — must not raise.
    _register_self_comms_node(port=8642)


def test_maybe_sync_on_start_no_peers_is_quiet(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _maybe_sync_on_start

    # Act + Assert — must not raise even though there are no peers.
    _maybe_sync_on_start()


def test_maybe_sync_on_start_respects_disable_flag(
    tmp_path: Path, db_path: Path, env_save_restore
) -> None:
    # Arrange — config explicitly disables sync_on_start.
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "host": {"canonical": "h"},
                "peers": {"peer1": {"ssh": "peer1-host"}},
                "comms_nodes": {"sync_on_start": False},
            }
        )
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    from scitex_agent_container.cli_pkg.listen_cmds import _maybe_sync_on_start

    # Act + Assert — must not raise (would otherwise attempt ssh).
    _maybe_sync_on_start()
