"""``export_state(tables=...)`` and ``sac db export --tables``.

The filter was added for ``sac registry sync``, which shipped only the
``comms_nodes`` delta. Both are gone as of 2026-08-28 — the directory moved
to the shared PostgreSQL store, so there is no slice to ship and no verb to
ship it. The filter itself stays useful for any subset of the tables that
remain, and these tests now exercise it with ``lineage`` / ``instances``.

``comms_nodes`` is asserted here in ONE direction only: that asking for it
now FAILS. A filter that still accepted the name would emit an empty array
and read as "sac exports the directory, and it is empty" — the
success-shaped answer this whole migration exists to remove.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.state_db import KNOWN_TABLES


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


# Parametrized over KNOWN_TABLES ITSELF, not over a copy of it. This was a
# hand-written literal list until 2026-08-28, and by then it had drifted: it
# still named ``turns`` / ``errors`` / ``heartbeats`` after the diary moved to
# PostgreSQL and those names left KNOWN_TABLES, so the test failed asserting
# the export SHOULD contain tables sac no longer has. A literal copy of a
# constant is a second source of truth that nothing keeps honest; reading the
# constant means this test tracks the whitelist for free, in both directions.
@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_export_state_no_tables_filter_includes_table(
    db_path: Path, table: str
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    payload = export_state()
    # Assert
    assert table in payload["tables"]


def test_export_state_tables_filter_emits_the_named_tables_rows(
    db_path: Path,
) -> None:
    # Arrange — the second table is now ``events`` rather than ``instances``:
    # that one left KNOWN_TABLES on 2026-08-28 when it moved to the shared
    # store, so it can no longer stand in for "a table the filter excludes".
    from scitex_agent_container._state.state_db import export_state, open_db
    from scitex_agent_container._state.state_db_nodes import record_lineage

    record_lineage(child="child-a", parent="parent-a", db_path=db_path)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO events (ts, kind, actor) VALUES (?, 'start', 'sac')",
            ("2026-08-28T00:00:00Z",),
        )
    # Act
    payload = export_state(tables=["lineage"])
    # Assert
    assert len(payload["tables"]["lineage"]) == 1


def test_export_state_tables_filter_excludes_unlisted_tables(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state, open_db
    from scitex_agent_container._state.state_db_nodes import record_lineage

    record_lineage(child="child-a", parent="parent-a", db_path=db_path)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO events (ts, kind, actor) VALUES (?, 'start', 'sac')",
            ("2026-08-28T00:00:00Z",),
        )
    # Act
    payload = export_state(tables=["lineage"])
    # Assert
    assert payload["tables"]["events"] == []


def test_export_state_rejects_comms_nodes_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert — an empty array would read as "the directory is empty".
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["comms_nodes"])


def test_export_state_rejects_instances_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange — the same ruling, and the widest blast radius of any table to
    # leave: an empty array here would read as "no agent has ever run on this
    # host", which is the first question an operator asks this export.
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["instances"])


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
    from scitex_agent_container._state.state_db_nodes import record_lineage
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_lineage(child="child-a", parent="parent-a", db_path=db_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "lineage"])
    # Assert
    assert result.exit_code == 0, result.output


def test_db_export_tables_flag_emits_only_named_table(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_nodes import record_lineage
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_lineage(child="child-a", parent="parent-a", db_path=db_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "lineage"])
    payload = json.loads(result.stdout)
    # Assert
    assert len(payload["tables"]["lineage"]) == 1


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


def test_db_export_tables_flag_csv_includes_lineage(
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


def test_db_export_tables_flag_csv_includes_instances(
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


def test_db_export_tables_flag_rejects_comms_nodes(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "comms_nodes"])
    # Assert — the name must fail at parse time, not emit an empty array.
    assert result.exit_code == 2
