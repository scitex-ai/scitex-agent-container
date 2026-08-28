"""``export_state(tables=...)`` and ``sac db export --tables``.

The filter was added for ``sac registry sync``, which shipped only the
``comms_nodes`` delta. Both are gone as of 2026-08-28 — the directory moved
to the shared PostgreSQL store, so there is no slice to ship and no verb to
ship it. The filter itself stays useful for any subset of the tables that
remain, and these tests now exercise it with ``channel_events`` /
``instances`` — the two that are left.

They exercised it with ``lineage`` until 2026-08-28, when the spawn DAG
moved to the shared PostgreSQL store and left ``KNOWN_TABLES``. The subject
of these tests is the FILTER, not that particular table, so they were
repointed rather than deleted.

``comms_nodes`` and now ``lineage`` are each asserted here in ONE direction
only: that asking for them FAILS. A filter that still accepted the name
would emit an empty array and read as "sac exports this, and it is empty" —
the success-shaped answer this whole migration exists to remove. For
``lineage`` that reading would be the worst of the three, because an empty
edge set does not mean "no data", it means "every agent is a root", and a
root may spawn.
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
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(
        target="agent-a", event={"from_agent": "peer", "kind": "message", "ts": 1.0}
    )
    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["channel_events"])
    # Assert
    assert len(payload["tables"]["channel_events"]) == 1


def test_export_state_tables_filter_excludes_unlisted_tables(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(
        target="agent-a", event={"from_agent": "peer", "kind": "message", "ts": 1.0}
    )
    record_instance_start("agent-a", host="h1")
    # Act
    payload = export_state(tables=["channel_events"])
    # Assert
    assert payload["tables"]["instances"] == []


def test_export_state_rejects_comms_nodes_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert — an empty array would read as "the directory is empty".
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["comms_nodes"])


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
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container.cli_pkg.db_group import db_export

    persist_event(
        target="agent-a", event={"from_agent": "peer", "kind": "message", "ts": 1.0}
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "channel_events"])
    # Assert
    assert result.exit_code == 0, result.output


def test_db_export_tables_flag_emits_only_named_table(db_path: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container.cli_pkg.db_group import db_export

    persist_event(
        target="agent-a", event={"from_agent": "peer", "kind": "message", "ts": 1.0}
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "channel_events"])
    payload = json.loads(result.stdout)
    # Assert
    assert len(payload["tables"]["channel_events"]) == 1


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


def test_db_export_tables_flag_csv_includes_channel_events(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "channel_events,instances"])
    payload = json.loads(result.stdout)
    # Assert
    assert "channel_events" in payload["tables"]


def test_db_export_tables_flag_rejects_lineage(
    db_path: Path,
) -> None:
    """The spawn DAG left KNOWN_TABLES on 2026-08-28.

    It must fail at PARSE time rather than emit an empty array. An empty
    ``lineage`` does not read as "no data" to anything downstream — it
    reads as "every agent is a root", and a root may spawn.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "lineage"])
    # Assert
    assert result.exit_code == 2


def test_export_state_rejects_lineage_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert — see the CLI twin above for why an empty array is worse.
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["lineage"])


def test_db_export_tables_flag_csv_includes_instances(
    db_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "channel_events,instances"])
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
