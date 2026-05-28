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


def test_export_state_no_tables_filter_includes_everything(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        KNOWN_TABLES,
        export_state,
    )

    # Act
    payload = export_state()
    # Assert — every known table appears as a key (rows may be empty).
    for table in KNOWN_TABLES:
        assert table in payload["tables"]


def test_export_state_tables_filter_restricts_to_named_only(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state
    from scitex_agent_container._state.state_db_nodes import register_comms_node

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    # also seed something into a non-selected table
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["comms_nodes"])
    # Assert — comms_nodes carries its row; instances is empty in the dump.
    assert len(payload["tables"]["comms_nodes"]) == 1
    assert payload["tables"]["instances"] == []


def test_export_state_tables_filter_unknown_table_raises(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act + Assert
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["not_a_real_table"])


# ---------------------------------------------------------------------------
# sac db export --tables — CLI
# ---------------------------------------------------------------------------


def test_db_export_tables_flag_emits_only_named_table(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_nodes import register_comms_node
    from scitex_agent_container.cli_pkg.db_group import db_export

    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes"])
    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["tables"]["comms_nodes"]) == 1


def test_db_export_tables_flag_unknown_name_bad_parameter(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "not_a_real_table"])
    # Assert — click.BadParameter raises a usage error, exit code 2.
    assert result.exit_code == 2
    assert "not_a_real_table" in result.output


def test_db_export_tables_flag_accepts_csv_multiple(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(
        db_export, ["--tables", "comms_nodes,instances"]
    )
    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "comms_nodes" in payload["tables"]
    assert "instances" in payload["tables"]
