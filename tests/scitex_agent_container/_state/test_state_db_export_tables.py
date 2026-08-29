"""``export_state(tables=...)`` and ``sac db export --tables``.

The filter was added for ``sac registry sync``, which shipped only the
``comms_nodes`` delta. Both are gone as of 2026-08-28 — the directory moved
to the shared PostgreSQL store, so there is no slice to ship and no verb to
ship it. The filter itself stays useful for any subset of the tables that
remain — and as of 2026-08-28 NONE do. ``instances`` was the last name in
``KNOWN_TABLES``, and it moved to the shared PostgreSQL store; the tuple is
EMPTY.

That is why every ACCEPTANCE case below became a REFUSAL case. The filter
still has to behave correctly, and with nothing to accept its whole
observable behaviour is what it rejects and how loudly — which is the half
that protects an operator, because an accepted-but-empty export is the
answer that reads as "this host has no history".

They exercised it with ``lineage`` until 2026-08-28, then ``channel_events``
/ ``instances`` for the rest of that day. The subject of these tests is the
FILTER, not any particular table, so they are repointed rather than deleted.

ONE CASUALTY OF THE ONE-TABLE WHITELIST, named rather than quietly dropped:
``test_export_state_tables_filter_excludes_unlisted_tables`` asserted that a
table NOT named in the filter comes back empty. With a single name in
``KNOWN_TABLES`` there is no second table to leave out, so that direction is
no longer expressible. It is replaced by the refusal assertion below rather
than deleted silently, and it should come back the moment a second table
exists.

``comms_nodes``, ``lineage`` and now ``channel_events`` are each asserted
here in ONE direction only: that asking for them FAILS. A filter that still accepted the name
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


def test_export_state_tables_filter_has_no_name_left_to_accept(
    db_path: Path,
) -> None:
    """The acceptance case, inverted — there is nothing left to name.

    It emitted ``instances`` rows until 2026-08-28. With ``KNOWN_TABLES``
    empty the filter can only reject, and rejecting is the behaviour worth
    protecting: an ACCEPTED name would produce ``{"instances": []}``, which
    an operator reads as "this host has no lifecycle history" while
    PostgreSQL holds all of it.
    """
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    raised = False
    # Act
    try:
        export_state(tables=["instances"])
    except ValueError:
        raised = True
    # Assert
    assert raised is True


def test_export_state_rejects_channel_events_now_that_it_moved(
    db_path: Path,
) -> None:
    """Replaces the excludes-unlisted-tables case — see the module docstring.

    An empty array here would read as "this agent has no waiting messages",
    which is exactly what a healthy undelivered inbox looks like. The channel
    history is ``sac_channel_events`` in the shared PostgreSQL now and this
    SQLite exporter cannot see it, so the name must fail rather than answer.
    """
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    # Act
    # Assert
    with pytest.raises(ValueError, match="unknown table"):
        export_state(tables=["channel_events"])


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


def test_db_export_tables_flag_exits_zero_with_no_filter(db_path: Path) -> None:
    """The happy path is now the UNFILTERED export.

    ``--tables instances`` exited zero until 2026-08-28. No name is
    acceptable any more, so the zero-exit case this file needs as a control
    — otherwise every assertion below is "it fails", which a totally broken
    command also satisfies — is the export with no ``--tables`` at all.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, [])
    # Assert
    assert result.exit_code == 0, result.output


def test_db_export_with_no_filter_emits_an_empty_tables_map(
    db_path: Path,
) -> None:
    """The unfiltered export carries NOTHING, and says so structurally.

    It emitted the named table's rows until 2026-08-28. With
    ``KNOWN_TABLES`` empty the payload's ``tables`` map is empty — which is
    the honest wire shape for "sac owns no SQLite tables" and is
    distinguishable from ``{"instances": []}``, the shape that would have
    read as "this host has no history".
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, [])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["tables"] == {}


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


def test_db_export_tables_flag_rejects_channel_events(
    db_path: Path,
) -> None:
    """The CLI twin of the ``export_state`` refusal above.

    It must fail at PARSE time rather than emit an empty array: this flag is
    what a peer would call, and "channel_events: []" reads as a delivered
    inbox.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "channel_events"])
    # Assert
    assert result.exit_code == 2


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


def test_db_export_tables_flag_rejects_instances_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange — this was ``..._csv_includes_instances`` until 2026-08-28,
    # asserting that a two-name CSV carried both. ``instances`` moved to the
    # shared store and left KNOWN_TABLES, which leaves NO known table — so
    # there is no CSV to carry and the honest assertion is the refusal. It is
    # also the refusal with the widest blast radius: an empty ``instances``
    # array in an export reads as "no agent has ever run on this host" while
    # PostgreSQL holds the fleet's whole lifecycle history.
    #
    # ``result.stdout`` is NOT parsed: a BadParameter writes the message to
    # stderr and prints no JSON, so parsing it would fail for a reason that
    # has nothing to do with the property under test.
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--tables", "instances"])
    # Assert
    assert result.exit_code != 0


def test_export_state_rejects_instances_now_that_it_moved(
    db_path: Path,
) -> None:
    # Arrange — the library-level half of the refusal above.
    from scitex_agent_container._state.state_db import export_state

    raised = False
    # Act
    try:
        export_state(tables=["instances"])
    except ValueError:
        raised = True
    # Assert
    assert raised is True


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
