"""ADR-0014 — ``export_state(tables=...)`` and ``sac db export --tables``.

Covers the new ``--tables TABLE[,TABLE...]`` filter that
``sac registry sync`` relies on to ship only the comms_nodes delta.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db pinned via env (mirrors test_db_group fixture)."""
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


# ---------------------------------------------------------------------------
# export_state(tables=...) — Python API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "definitions",
        "instances",
        "instance_heartbeats",
        "events",
        "attempts",
        "turns",
        "errors",
        "heartbeats",
        "channel_events",
        "node_tokens",
        "lineage",
        "comms_grants",
        "comms_nodes",
    ],
)
def test_export_state_no_tables_filter_includes_table(
    db_path: Path, table: str
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    payload = export_state()
    # Assert
    assert table in payload["tables"]


def test_export_state_tables_filter_emits_comms_nodes_row(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_nodes import (
        register_comms_node,
    )

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["comms_nodes"])
    # Assert
    assert len(payload["tables"]["comms_nodes"]) == 1


def test_export_state_tables_filter_excludes_unlisted_tables(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_nodes import (
        register_comms_node,
    )

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["comms_nodes"])
    # Assert
    assert payload["tables"]["instances"] == []


def test_export_state_tables_filter_unknown_table_raises(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["not_a_real_table"])


# ---------------------------------------------------------------------------
# sac db export --tables — CLI
# ---------------------------------------------------------------------------


def test_db_export_tables_flag_exits_zero(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_nodes import (
        register_comms_node,
    )
    from scitex_agent_container.cli_pkg.db_group import db_export

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes"])
    # Assert
    assert result.exit_code == 0, result.output


def test_db_export_tables_flag_emits_only_named_table(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_nodes import (
        register_comms_node,
    )
    from scitex_agent_container.cli_pkg.db_group import db_export

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes"])
    payload = json.loads(result.stdout)
    # Assert
    assert len(payload["tables"]["comms_nodes"]) == 1


def test_db_export_tables_flag_unknown_name_exits_two(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "not_a_real_table"])
    # Assert
    assert result.exit_code == 2


def test_db_export_tables_flag_unknown_name_names_offender_in_output(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "not_a_real_table"])
    # Assert
    assert "not_a_real_table" in result.output


def test_db_export_tables_flag_csv_includes_comms_nodes(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes,instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert "comms_nodes" in payload["tables"]


def test_db_export_tables_flag_csv_includes_instances(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes,instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert "instances" in payload["tables"]
