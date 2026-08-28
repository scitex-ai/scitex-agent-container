"""F-CS14 — ``export_state`` / ``import_state`` and their CLI.

Extracted from ``test_state_db.py`` on 2026-08-28 and RE-POINTED, because
the table every one of these tests used to exercise is gone from this
path. ``instances`` and ``events`` moved to per-host PostgreSQL and left
``KNOWN_TABLES``; the ssh-and-JSON export no longer carries them, and the
store's own federation replicates them instead.

WHAT THAT MEANS FOR EACH TEST, stated rather than quietly patched:

  * ``test_export_state_payload_instances_table_contains_recorded_row_name``
    and ``test_export_state_filters_out_instance_rows_older_than_since_
    cutoff`` asserted on ``payload["tables"]["instances"]``. That key does
    not exist any more, so both were DELETED rather than rewritten — the
    thing they measured is not a property of this module now. The
    ``--since`` filter itself still has coverage below on ``definitions``,
    which is still SQLite and still exported.
  * The wire-shape tests (``schema`` / ``host`` / ``since`` /
    ``exported_at`` / the ``tables`` key set) are unchanged in substance:
    they never depended on WHICH table carried a row, only that one did.
    They now seed ``comms_nodes``.
  * The three round-trip tests (source → payload → sink) counted
    ``inserted["instances"]``. The property — an import into a fresh
    database inserts each row once and a replay inserts none — is
    identical on ``comms_nodes``, which is where it is now measured.

Keeping a test GREEN on a table it can no longer reach would have been
the worse outcome: a name claiming to cover the instances export, passing
forever, with nothing forcing anyone to look.

PA-306: no mocks; real on-disk SQLite under ``tmp_path``. No ``pg_schema``
here on purpose — every table this file touches is still SQLite, and a
PostgreSQL dependency would make these skip on hosts that have no cluster
for no reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.state_db import KNOWN_TABLES


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env so the CLI picks it up."""
    import importlib

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


def _seed(db: Path, name: str = "polish-clew", host: str = "src-host") -> None:
    """One exportable row, on a table that is still SQLite."""
    from scitex_agent_container._state.state_db_nodes import register_comms_node

    register_comms_node(name=name, host=host, a2a_port=7000, db_path=db)


def _expected_schema_version():
    from scitex_agent_container._state.state_db import EXPORT_SCHEMA_VERSION

    return EXPORT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# the payload's self-describing envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, expected_factory",
    [
        ("schema", _expected_schema_version),
        ("host", lambda: "src-host"),
        ("since", lambda: None),
    ],
)
def test_export_state_payload_carries_self_describing_field(
    db_path: Path, field: str, expected_factory
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path)
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert payload[field] == expected_factory()


def test_export_state_payload_includes_exported_at_timestamp(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path)
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert "exported_at" in payload


def test_export_state_payload_tables_key_covers_all_known_tables(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path)
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert set(payload["tables"]) == set(KNOWN_TABLES)


def test_export_state_payload_has_no_key_for_a_migrated_table(db_path: Path):
    # Arrange — the honest successor to the deleted instances-payload test.
    # An ABSENT key is a different claim from an empty list: it says sac
    # does not export this table any more, rather than "there were no
    # agents".
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path)
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert "instances" not in payload["tables"]


def test_export_state_carries_the_seeded_row(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path)
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert [r["name"] for r in payload["tables"]["comms_nodes"]] == ["polish-clew"]


def test_export_state_since_cutoff_drops_a_row_stamped_before_it(db_path: Path):
    # Arrange — ``definitions`` filters on ``first_seen_at``, an ISO string,
    # so the cutoff can be pinned exactly instead of raced against the wall
    # clock the way the deleted instances version had to be.
    from scitex_agent_container._state.state_db import export_state, open_db

    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO definitions (id, name, yaml_path, yaml_sha256, "
            "scope, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("d-old", "old", "/o.yaml", "sha-o", "global", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO definitions (id, name, yaml_path, yaml_sha256, "
            "scope, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("d-new", "new", "/n.yaml", "sha-n", "global", "2026-06-01T00:00:00Z"),
        )
    # Act
    payload = export_state(since="2026-03-01T00:00:00Z", host="h")
    # Assert
    assert [r["name"] for r in payload["tables"]["definitions"]] == ["new"]


# ---------------------------------------------------------------------------
# round trip: source -> payload -> a FRESH sink database
# ---------------------------------------------------------------------------


@pytest.fixture
def switch_to_sink_db(tmp_path: Path):
    """Factory fixture: caller invokes after preparing source-db state.

    Yields a callable ``switch()`` that swaps the env-rooted state.db to a
    fresh sink path under tmp_path, reloads the module, and returns the
    reloaded ``state_db`` module for use against the sink. On test
    teardown the env is restored and the module reloaded back to the
    pre-switch path. This sequencing matters: tests need to record rows
    on the SOURCE db, snapshot them, and only then point the env at the
    sink — otherwise the "import" runs against a db that already holds
    the rows it's trying to insert.
    """
    import importlib

    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    switched = False

    def switch():
        nonlocal switched
        os.environ[key] = str(tmp_path / "sink.db")
        import scitex_agent_container._state.state_db as mod

        importlib.reload(mod)
        switched = True
        return mod

    try:
        yield switch
    finally:
        if switched:
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
            import scitex_agent_container._state.state_db as mod

            importlib.reload(mod)


def test_import_state_into_a_fresh_db_inserts_each_source_row(
    db_path: Path, switch_to_sink_db
):
    # Arrange — write 2 rows on the source db and snapshot them.
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path, name="a", host="src")
    _seed(db_path, name="b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    # Act — import into the fresh sink (different env path).
    inserted = sink_mod.import_state(payload)
    # Assert
    assert inserted["comms_nodes"] == 2


def test_import_state_replayed_on_the_same_payload_inserts_zero_rows(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path, name="a", host="src")
    _seed(db_path, name="b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    sink_mod.import_state(payload)
    # Act
    inserted_again = sink_mod.import_state(payload)
    # Assert
    assert inserted_again["comms_nodes"] == 0


def test_import_state_round_trip_lands_exactly_the_source_names_on_the_sink(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path, name="a", host="src")
    _seed(db_path, name="b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    # Act
    sink_mod.import_state(payload)
    with sink_mod.open_db() as conn:
        names = sorted(
            r["name"] for r in conn.execute("SELECT name FROM comms_nodes").fetchall()
        )
    # Assert
    assert names == ["a", "b"]


def test_import_state_rejects_a_payload_with_an_unknown_schema_version(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import import_state

    bad_payload = {"schema": 999, "tables": {}}
    # Act
    ctx = pytest.raises(ValueError, match="schema")
    # Assert
    with ctx:
        import_state(bad_payload)


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def test_db_export_via_cli_emits_json_with_the_recorded_row(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    _seed(db_path, name="x", host="h")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h"])
    payload = json.loads(result.stdout)
    # Assert
    assert len(payload["tables"]["comms_nodes"]) == 1


def test_db_export_via_cli_emits_payload_with_requested_host_key(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_export

    _seed(db_path, name="x", host="h")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["host"] == "h"


def test_db_import_via_cli_reads_stdin_and_inserts_one_row(
    db_path: Path, switch_to_sink_db
):
    # Arrange — snapshot source, point CLI at the fresh sink db.
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path, name="x", host="h")
    payload = export_state(host="h")
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, ["-", "--json"], input=json.dumps(payload))
    body = json.loads(result.stdout)
    # Assert
    assert body["inserted"]["comms_nodes"] == 1


def test_db_import_via_cli_echoes_payload_host_back_in_json_body(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    _seed(db_path, name="x", host="h")
    payload = export_state(host="h")
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, ["-", "--json"], input=json.dumps(payload))
    body = json.loads(result.stdout)
    # Assert
    assert body["host"] == "h"
