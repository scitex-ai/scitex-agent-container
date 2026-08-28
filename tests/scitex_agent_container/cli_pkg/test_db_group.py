"""Tests for ``sac db`` group console output and export branches.

Coverage closure for ``scitex_agent_container.cli_pkg.db_group``.
Targets uncovered rich-console paths (db_show / db_query / db_migrate /
db_clean / db_import) plus the export ``--dry-run`` / ``--output``
branches and the import file-path + dry-run branches.

PA-306 conventions:

* No mocks. Real ``CliRunner`` against the real click commands. Real
  on-disk SQLite ``state.db`` rooted at ``tmp_path`` via the
  ``SCITEX_AGENT_CONTAINER_STATE_DB`` env var (the same seam the
  ``_state/test_state_db.py`` suite already uses).
* AAA structure, one assertion per test, 3+ word descriptive names.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location pinned via env, reloaded on teardown."""
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
# db show — non-JSON console branch (lines 65-68)
# ---------------------------------------------------------------------------


def test_db_show_console_output_starts_with_state_db_header(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, [])
    # Assert
    assert "sac state.db" in result.output


def test_db_show_console_output_lists_no_table_row(db_path: Path):
    """``sac db show`` has nothing to count, and must not invent a line.

    It listed the ``instances`` row until 2026-08-28, then
    ``channel_events``; both moved to the shared PostgreSQL store and
    ``KNOWN_TABLES`` is empty. A printed ``instances 0`` would be the exact
    wrong answer — it reads as "no agent has ever run here" while PostgreSQL
    holds the fleet's whole lifecycle history — so the honest rendering
    prints the header and no counts at all.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, [])
    # Assert
    assert "instances" not in result.output


# ---------------------------------------------------------------------------
# db show — names the store it read
#
# INCIDENT 2026-08-09: SCITEX_AGENT_CONTAINER_STATE_DB is set per-agent in
# every sac container, so an agent calling `db show` reads its OWN shard,
# which never holds fleet rows. All-zero counts then look identical to a
# wiped fleet registry, and two agents independently escalated P1 data
# loss from their own empty shard while the host DB was healthy. The
# payload must say WHICH database produced the numbers.
# ---------------------------------------------------------------------------


def test_db_show_json_payload_names_the_store_it_read(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, ["--json"])
    # Assert
    assert json.loads(result.stdout)["store"] == str(db_path)


def test_db_show_console_output_names_the_store_it_read(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, [])
    # Assert
    assert db_path.name in result.output


def test_db_query_refuses_every_name_because_none_is_known(db_path: Path):
    """``--table`` is a ``click.Choice(KNOWN_TABLES)`` over an EMPTY tuple.

    Eight tests here drove ``--table instances`` (and, before it,
    ``--table channel_events``) through the console, the JSON, the MCP
    wrapper, the ``--where`` fragment and the empty-table rendering. All of
    those tables moved to the shared PostgreSQL store on 2026-08-28, so the
    verb can no longer name anything and every one of those renderings is
    unreachable.

    THE REFUSAL IS THE PROPERTY WORTH KEEPING, and it is not a formality:
    had the names been left whitelisted, this command would print ``[]`` for
    a table PostgreSQL is holding rows for, and an empty array reads as "this
    agent has nothing" rather than "you are asking the wrong database".
    ``sac agents list`` is the verb that answers the question this one used
    to. The store-provenance guarantees those tests protected (the bare JSON
    array, the MCP sibling ``store`` key) are exercised by ``db show``, which
    still runs.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "instances", "--limit", "5"])
    # Assert
    assert result.exit_code != 0


def test_db_query_refusal_names_the_offending_table(db_path: Path):
    # Arrange — an operator who typed a table name needs to see WHICH name
    # was refused, not just that something was.
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "instances", "--limit", "5"])
    # Assert
    assert "instances" in result.output


# ---------------------------------------------------------------------------
# db migrate — default registry_dir resolution + console branch
# (lines 168, 178)
# ---------------------------------------------------------------------------


def test_db_migrate_resolves_registry_dir_from_env_variable(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "legacy-reg"
    reg.mkdir()
    (reg / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "config": "/dev/null/alpha.yaml",
                "pid": 1,
                "started_at": "2026-05-05T03:29:41Z",
                "screen": "alpha",
            }
        )
    )
    key = "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
    saved = os.environ.get(key)
    os.environ[key] = str(reg)
    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    try:
        # Act
        result = runner.invoke(db_migrate, ["--host", "h", "--json"])
        body = json.loads(result.stdout)
        # Assert
        assert body == {"registry_dir": str(reg), "imported": 1, "skipped": 0}
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_db_migrate_console_output_reports_imported_count(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "x.json").write_text(
        json.dumps(
            {
                "name": "x",
                "config": "/dev/null/x.yaml",
                "pid": 2,
                "started_at": "2026-05-05T03:29:42Z",
                "screen": "x",
            }
        )
    )
    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    # Act
    result = runner.invoke(db_migrate, ["--registry-dir", str(reg), "--host", "h"])
    # Assert
    assert "imported=1" in result.output


# ---------------------------------------------------------------------------
# db clean — non-JSON console branch (lines 240-245)
# ---------------------------------------------------------------------------


@pytest.fixture
def dead_pid_environment():
    """Pin host + suppress proc-btime so a fake pid reaps as crashed."""
    from scitex_agent_container._state import state_db

    saved_host = os.environ.get("SAC_HOST")
    saved_btime = state_db._proc_btime
    os.environ["SAC_HOST"] = "test-host"
    state_db._proc_btime = lambda: None  # type: ignore[assignment]
    try:
        yield
    finally:
        state_db._proc_btime = saved_btime  # type: ignore[assignment]
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host


def test_db_clean_console_output_emits_swept_label_header(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, [])
    # Assert
    assert "swept=" in result.output


def test_db_clean_dry_run_console_uses_would_sweep_label(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, ["--dry-run"])
    # Assert
    assert "would-sweep=" in result.output


def test_db_clean_console_lists_crashed_counter_when_nonzero(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, [])
    # Assert
    assert "crashed" in result.output


# ---------------------------------------------------------------------------
# db export — --dry-run and --output branches (lines 337-349, 354-355)
# ---------------------------------------------------------------------------


def test_db_export_dry_run_reports_an_empty_row_counts_map(db_path: Path):
    """The dry run's counts map is empty, not a zero per table."""
    # Arrange — no table to seed; see the module docstring.
    from scitex_agent_container.cli_pkg.db_group import db_export

    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h", "--dry-run"])
    body = json.loads(result.stdout)
    # Assert
    assert body["row_counts"] == {}


def test_db_export_dry_run_echoes_host_stamp_into_body(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container.cli_pkg.db_group import db_export

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h", "--dry-run"])
    body = json.loads(result.stdout)
    # Assert
    assert body["host"] == "h"


def test_db_export_with_output_writes_json_blob_to_file(db_path: Path, tmp_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container.cli_pkg.db_group import db_export

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    out = tmp_path / "nested" / "dump.json"
    runner = CliRunner()
    # Act
    runner.invoke(db_export, ["--host", "h", "--output", str(out)])
    payload = json.loads(out.read_text())
    # Assert
    assert payload["host"] == "h"


def test_db_export_with_output_creates_parent_directory_on_disk(
    db_path: Path, tmp_path: Path
):
    # Arrange
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container.cli_pkg.db_group import db_export

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    out = tmp_path / "new-subdir" / "dump.json"
    runner = CliRunner()
    # Act
    runner.invoke(db_export, ["--host", "h", "--output", str(out)])
    # Assert
    assert out.parent.is_dir()


# ---------------------------------------------------------------------------
# db import — file path + dry-run console + non-JSON inserted output
# (lines 403, 406-432, 447-454)
# ---------------------------------------------------------------------------


@pytest.fixture
def switch_to_sink_db(tmp_path: Path):
    """Swap env-rooted state.db to a fresh sink for import tests."""
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


def test_db_import_reads_payload_from_filesystem_path(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    """The FILE-path branch still parses and succeeds, carrying nothing."""
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump), "--json"])
    body = json.loads(result.stdout)
    # Assert
    assert body["inserted"] == {}


def test_db_import_dry_run_json_reports_an_empty_would_insert_map(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    """The dry run's preview is empty, not a zero per table."""
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump), "--dry-run", "--json"])
    body = json.loads(result.stdout)
    # Assert
    assert body["would_insert"] == {}


def test_db_import_dry_run_json_flags_payload_as_dry_run(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump), "--dry-run", "--json"])
    body = json.loads(result.stdout)
    # Assert
    assert body["dry_run"] is True


def test_db_import_dry_run_console_reports_would_insert_total(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump), "--dry-run"])
    # Assert
    assert "would-insert=" in result.output


def test_db_import_dry_run_console_lists_no_table_count(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    """The console rendering prints no per-table line, for the same reason
    ``db show`` does not: a printed zero would be a claim about the fleet."""
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump), "--dry-run"])
    # Assert
    assert "instances" not in result.output


def test_db_import_console_output_reports_inserted_total(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    # Arrange
    from scitex_agent_container._state.state_db import export_state
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(target="x", event={"from_agent": "p", "kind": "message", "ts": 1.0})
    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump)])
    # Assert
    assert "inserted=" in result.output


def test_db_import_console_output_lists_no_inserted_count(
    db_path: Path, switch_to_sink_db, tmp_path: Path
):
    """Same for the committed import."""
    # Arrange
    from scitex_agent_container._state.state_db import export_state

    payload = export_state(host="h")
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(payload))
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, [str(dump)])
    # Assert
    assert "instances" not in result.output

