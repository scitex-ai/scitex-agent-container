"""ADR-0014 — listen-startup registers the operator identity.

The hook lives in :mod:`scitex_agent_container.cli_pkg.listen_cmds`
(``_register_self_comms_node``); this test exercises it directly without
spinning a real uvicorn (the ``sac listen`` entry point flows through the
same helper, but binding a real port + running the ASGI server is out of
scope for a unit test).

``_maybe_sync_on_start`` was exercised here until 2026-08-28 and is GONE
along with ``sac registry sync``: the directory moved to the shared
PostgreSQL store, so there is no per-host copy to converge and no peer to
pull from. Its two tests asserted that the sweep wrote no rows in the
no-peers and opt-out cases — a claim that no longer has a subject.

Real config.yaml + a real store via ``pg_schema``; no mocks.
"""

from __future__ import annotations

import importlib
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
    db_path: Path, cfg_with_lead: Path, pg_schema: str
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
    db_path: Path, cfg_with_lead: Path, pg_schema: str
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=9000)
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["a2a_port"] == 9000


def test_register_self_no_lead_block_writes_no_row(
    db_path: Path, cfg_no_lead: Path, pg_schema: str
) -> None:
    # Arrange — pin the "no row written" half of the contract; the
    # warning-emission half is its own test below so each assertion
    # stays single-fact.
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=8642)
    # Assert
    assert list_comms_nodes() == []


def test_register_self_no_lead_block_warns_loudly_on_stderr(
    db_path: Path, cfg_no_lead: Path, capsys: pytest.CaptureFixture[str], pg_schema: str
) -> None:
    # Arrange — the pre-PR4 behaviour was a silent return: an operator
    # whose listen failed to advertise `lead` had no diagnostic. The
    # repair-verb PR#308 closed half the gap; this PR fills the rest
    # by emitting a loud `# WARN:` line + the repair recipe so an
    # operator grepping listen stderr finds an actionable pointer.
    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=8642)
    captured = capsys.readouterr()
    # Assert — both the diagnostic + the repair-verb hint are surfaced.
    assert (
        "no `lead:` block" in captured.err and "sac registry register" in captured.err
    )


def test_register_self_records_this_host_as_the_origin(
    db_path: Path, cfg_with_lead: Path, pg_schema: str
) -> None:
    # Arrange
    import socket

    from scitex_agent_container.cli_pkg.listen_cmds import _register_self_comms_node

    # Act
    _register_self_comms_node(port=8642)
    # Assert — provenance is the store's ``_origin``, stamped from the
    # writing node, where the old ``source_host`` column held NULL.
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["source_host"] == socket.gethostname()


def test_register_self_with_missing_config_writes_no_row(
    db_path: Path, tmp_path: Path, env_save_restore, pg_schema: str
) -> None:
    # Arrange — point config to a non-existent file. ``host_config.load``
    # is missing-tolerant so the hook should land in the "no lead"
    # silent-noop branch and write no row.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "absent.yaml"))
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes
    from scitex_agent_container.cli_pkg.listen_cmds import (
        _register_self_comms_node,
    )

    # Act
    _register_self_comms_node(port=8642)
    rows = list_comms_nodes()
    # Assert
    assert rows == []
