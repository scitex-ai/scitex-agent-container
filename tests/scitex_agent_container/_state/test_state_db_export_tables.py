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


# MEASURED ON ``instances``, NOT ON ``comms_nodes``, SINCE 2026-08-28.
#
# These two tests used to seed a row with ``register_comms_node`` and assert
# the filter emitted it. That primitive now writes PostgreSQL, so the SQLite
# table it exports from is empty no matter what the filter does — the test
# would measure nothing while still being named for what it once measured.
# The PROPERTY (a --tables filter emits the named table and only the named
# table) is unchanged and is measured here on ``instances``, which is still
# SQLite and still exported.
def test_export_state_tables_filter_emits_the_named_tables_rows(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["instances"])
    # Assert
    assert len(payload["tables"]["instances"]) == 1


def test_export_state_tables_filter_excludes_unlisted_tables(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["instances"])
    # Assert
    assert payload["tables"]["lineage"] == []


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
    # Arrange — seeded through ``instances`` rather than
    # ``register_comms_node``: the latter now writes PostgreSQL, so under
    # the suite's store guard it raises instead of seeding anything.
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_instance_start("agent-a", host="h1")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "instances"])
    # Assert
    assert result.exit_code == 0, result.output


def test_db_export_tables_flag_emits_only_named_table(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_instance_start("agent-a", host="h1")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert len(payload["tables"]["instances"]) == 1


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


# The CSV form is measured on two tables that are STILL SQLite. It used to
# name ``comms_nodes``, which since 2026-08-28 exports an abandoned table:
# the key is still emitted (KNOWN_TABLES keeps the name), so the assertion
# would keep passing while naming the one table for which the flag no longer
# ships anything.
def test_db_export_tables_flag_csv_includes_the_first_named_table(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "lineage,instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert "lineage" in payload["tables"]


def test_db_export_tables_flag_csv_includes_the_second_named_table(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "lineage,instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert "instances" in payload["tables"]
