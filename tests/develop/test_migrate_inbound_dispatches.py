#!/usr/bin/env python3
"""What the two dispatch migrations STAMP on a row, asserted without a server.

WHY A SEPARATE FILE FROM THE DIRECTIONAL TESTS.
``test_migrate_scripts_do_not_write_by_default.py`` answers one question for
every migration script — "does a bare invocation reach the store?" — with an
unreachable DSN as the instrument. It answers it for
``migrate_inbound_dispatches_to_postgres.py`` too. It cannot answer THIS
question, because a script that never reaches the store also never reveals
what it would have written.

The mapping is where both of these scripts can be silently wrong, and the
two are wrong in OPPOSITE directions, which is the whole reason the pair is
tested together here:

* ``migrate_dispatches_to_postgres.py`` (OUTBOUND) has no ``agent`` column
  to read, so it stamps one. Before ``--agent`` existed it could only ever
  stamp ``""``, which is right for the main ``state.db`` and wrong for a
  per-agent shard — where the file's own directory name IS the scope.
* ``migrate_inbound_dispatches_to_postgres.py`` (INBOUND) has an ``agent``
  column that was ``TEXT NOT NULL`` from the first day, so it must carry
  what the row says and must NOT grow a flag that overrides it.

Both stamp an IMMUTABLE identity field. A row written under the wrong scope
is a different record forever and cannot be re-scoped in place, so "wrong
here" is not a re-runnable mistake.

NO MOCKS, NO MONKEYPATCH (PA-306 §3). The mapping functions are pure, so
they are called directly; the tests that need a whole ``main()`` build a real
SQLite file and read real stdout through ``capsys``, with ``sys.argv`` saved
and restored by hand. Nothing here reaches PostgreSQL — every one of these
paths is a dry run, which is precisely the property its sibling file pins.

LIVES IN tests/develop/, NOT the mirror tree — these scripts sit at the repo
root under ``scripts/`` with no ``src/`` counterpart, so a ``test_*.py``
under ``tests/<pkg>/`` is an orphan and PS-204 §2 fails the build.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(name: str):
    """Import a repo-root script by path, the way its siblings' tests do."""
    path = SCRIPTS / name
    if not path.exists():
        pytest.skip(f"{name} not present in this checkout")
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _argv(argv: list[str]):
    """Set ``sys.argv``, then put back exactly what was there. PA-306."""
    saved = list(sys.argv)
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = saved


INBOUND = "migrate_inbound_dispatches_to_postgres.py"
OUTBOUND = "migrate_dispatches_to_postgres.py"


def _inbound_sqlite(tmp_path: Path, rows: list[tuple]) -> Path:
    """A real ``inbound_dispatches`` table, with the DDL #1169 deleted."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE inbound_dispatches (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               agent TEXT NOT NULL, from_agent TEXT NOT NULL,
               dispatch_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
               ts REAL NOT NULL, reported_ts REAL)"""
    )
    conn.executemany(
        "INSERT INTO inbound_dispatches "
        "(agent, from_agent, dispatch_id, status, ts, reported_ts) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _outbound_sqlite(tmp_path: Path) -> Path:
    """One outbound dispatch, with the columns that script SELECTs."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE dispatches (
               dispatch_id TEXT PRIMARY KEY, from_agent TEXT, to_agent TEXT,
               conversation_id TEXT, text_summary TEXT, status TEXT,
               ts REAL NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO dispatches VALUES (?,?,?,?,?,?,?)",
        ("d-9", "alpha", "beta", "c1", "hi", "sent", 900.0),
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# INBOUND — the agent is IN THE ROW, and every field maps or is dropped
# ---------------------------------------------------------------------------


def test_inbound_record_carries_the_agent_recorded_in_the_row():
    """THE POINT OF THE WHOLE SCRIPT.

    Its outbound sibling writes ``agent=""`` because its table never had the
    column. This one's did, so a row that says ``beta`` must arrive as
    ``beta`` — not as ``""``, and not as ``from_agent`` (which is who SENT
    the wake, not whose ledger it landed in).
    """
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "pending", "ts": 1000.5, "reported_ts": None}
    # Act
    record = module._record(row)
    # Assert
    assert record["agent"] == "beta"


def test_inbound_record_maps_a_null_dispatch_id_to_the_empty_string():
    """``dispatch_id`` is optional at the call site and an IDENTITY field.

    ``inbound_ledger`` already rules that ``""`` is how "this wake carried
    no dispatch id" is spelled, which is what the SQLite ``NULL`` meant.
    """
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": None,
           "status": "pending", "ts": 1000.5, "reported_ts": None}
    # Act
    record = module._record(row)
    # Assert
    assert record["dispatch_id"] == ""


def test_inbound_record_omits_reported_ts_when_the_row_never_settled():
    """An unsettled row has no settle time, and inventing one would make
    "never reported" indistinguishable from "reported at epoch"."""
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "pending", "ts": 1000.5, "reported_ts": None}
    # Act
    record = module._record(row)
    # Assert
    assert "reported_ts" not in record


def test_inbound_record_carries_reported_ts_when_the_row_did_settle():
    """NEGATIVE CONTROL for the test above — a mapping that dropped
    ``reported_ts`` unconditionally would pass it and lose every settle
    time in the table."""
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "reported", "ts": 1000.5, "reported_ts": 1001.25}
    # Act
    record = module._record(row)
    # Assert
    assert record["reported_ts"] == 1001.25


def test_inbound_record_carries_a_reporting_status_verbatim():
    """``reporting`` is NOT rewound to ``pending``.

    Rewinding looks like a repair — ``claim_oldest_pending`` only ever
    claims ``pending``, so a row abandoned mid-report is stuck — but a
    ``reporting`` row may already have pushed its completion, so the rewind
    gambles a DOUBLE report against a missing one. A copy is the wrong place
    to take that bet.
    """
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "reporting", "ts": 1000.5, "reported_ts": None}
    # Act
    record = module._record(row)
    # Assert
    assert record["status"] == "reporting"


def test_inbound_record_never_raises_on_a_row_with_no_columns_at_all():
    """TOTALITY IS A REQUIREMENT, NOT DEFENSIVENESS.

    ``_migrate_lib.migrate_rows`` calls ``to_record`` OUTSIDE its per-row
    ``try``, so an exception in the mapping does not fail one row — it
    aborts the pass and strands every row after it, against the library's
    own promise that "one row's failure never aborts the pass". A host whose
    table is narrower than the column list produces exactly this shape,
    because ``SqliteSource`` intersects the columns with ``PRAGMA
    table_info`` rather than assuming them present.
    """
    # Arrange
    module = _load(INBOUND)
    # Act
    record = module._record({})
    # Assert
    assert record == {"dispatch_id": ""}


def test_inbound_key_never_raises_on_a_defective_record():
    """``key_of`` is called outside the same ``try``, so it is total too.

    The defective row then fails LOUDLY at the ``put``, inside the try,
    named in ``MigrationReport.failed`` — one row, not the pass.
    """
    # Arrange
    module = _load(INBOUND)
    # Act
    key = module._key({})
    # Assert
    assert key == {"agent": None, "from_agent": None,
                   "dispatch_id": None, "ts": None}


def test_inbound_key_is_exactly_the_stores_identity_fields():
    """Read from ``inbound_ledger.IDENTITY_FIELDS`` rather than re-typed, so
    the two cannot drift apart."""
    # Arrange
    from scitex_agent_container._state.inbound_ledger import IDENTITY_FIELDS
    module = _load(INBOUND)
    record = {"agent": "b", "from_agent": "a", "dispatch_id": "d",
              "ts": 1.0, "status": "pending"}
    # Act
    key = module._key(record)
    # Assert
    assert tuple(key) == IDENTITY_FIELDS


def test_inbound_source_reads_fifo_by_ts():
    """``claim_oldest_pending`` reads this ledger FIFO by ``ts``, so the
    dry-run listing must be readable in the same order."""
    # Arrange
    module = _load(INBOUND)
    # Act
    order_by = module.SOURCE.order_by
    # Assert
    assert order_by.startswith("ts ASC")


def test_inbound_source_selects_every_column_the_sqlite_table_had():
    """A column missing here is a column silently not migrated."""
    # Arrange
    module = _load(INBOUND)
    # Act
    columns = set(module.SOURCE.columns)
    # Assert
    assert columns == {"id", "agent", "from_agent", "dispatch_id",
                       "status", "ts", "reported_ts"}


def test_inbound_describe_marks_an_unfinished_row():
    """The 133 unfinished dispatches measured fleet-wide are the reason this
    migration is not merely history preservation — they must stand out in
    the listing an operator reads before committing."""
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "pending", "ts": 1000.5, "id": 7}
    # Act
    line = module._describe(row)
    # Assert
    assert line.startswith("UNFINISHED")


def test_inbound_describe_flags_a_status_no_reader_would_match():
    """``status`` is free TEXT in the store, so a value outside
    ``VALID_STATUSES`` migrates happily and then matches no reader's
    filter. Better seen in the dry run than never."""
    # Arrange
    module = _load(INBOUND)
    row = {"agent": "beta", "from_agent": "alpha", "dispatch_id": "d-1",
           "status": "half-done", "ts": 1000.5, "id": 7}
    # Act
    line = module._describe(row)
    # Assert
    assert line.startswith("UNKNOWN!")


def test_inbound_has_no_agent_flag():
    """THE DELIBERATE ASYMMETRY WITH THE SIBLING, pinned.

    An ``--agent`` here could only mean "overwrite the owner the row
    recorded", which fabricates the one fact this table did not lose. The
    script builds its parser through ``_migrate_lib.add_common_arguments``,
    so this asserts the shared CLI did not grow the flag either.
    """
    # Arrange
    module = _load(INBOUND)
    # Act — argparse rejects an unknown option with SystemExit(2)
    with _argv(["migrate"]):
        try:
            module.main(["--agent", "beta"])
            rejected = False
        except SystemExit:
            rejected = True
    # Assert
    assert rejected


def test_inbound_dry_run_counts_rows_that_collapse_to_one_record(tmp_path, capsys):
    """SQLite's AUTOINCREMENT kept same-instant wakes apart; the store's
    identity does not. The collapse is REPORTED so the verify line is not
    read as an unexplained shortfall."""
    # Arrange
    db = _inbound_sqlite(tmp_path, [
        ("alpha", "lead", None, "pending", 1002.5, None),
        ("alpha", "lead", None, "pending", 1002.5, None),
    ])
    module = _load(INBOUND)
    # Act
    with _argv(["migrate"]):
        module.main(["--db-path", str(db)])
    # Assert
    assert "1 row(s) share an identity" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# OUTBOUND — the agent is NOT in the row, so --agent supplies it
# ---------------------------------------------------------------------------


_OUTBOUND_ROW = {
    "dispatch_id": "d-9", "from_agent": "alpha", "to_agent": "beta",
    "conversation_id": "c1", "text_summary": "hi", "status": "sent",
    "ts": 900.0,
}


def test_outbound_record_defaults_to_the_unscoped_empty_string():
    """BACK-COMPAT, pinned. Every invocation written before ``--agent``
    existed must keep meaning what it meant: the main ``state.db`` never
    recorded a scope, and ``record_dispatch`` writes this same ``""``."""
    # Arrange
    module = _load(OUTBOUND)
    # Act
    default = inspect.signature(module.migrate).parameters["agent"].default
    # Assert
    assert default == ""


def test_outbound_record_stamps_the_agent_it_is_given():
    """A shard DOES know its agent — it is the directory the file sits in —
    so a shard sweep can carry the real scope instead of ``""``."""
    # Arrange
    module = _load(OUTBOUND)
    # Act
    record = module._record(_OUTBOUND_ROW, "beta")
    # Assert
    assert record["agent"] == "beta"


def test_outbound_record_leaves_every_other_field_alone():
    """NEGATIVE CONTROL — ``--agent`` must change the scope and nothing
    else, so a shard row and a main-db row differ in exactly one field."""
    # Arrange
    module = _load(OUTBOUND)
    # Act
    scoped = module._record(_OUTBOUND_ROW, "beta")
    unscoped = module._record(_OUTBOUND_ROW, "")
    # Assert
    assert {k: v for k, v in scoped.items() if k != "agent"} == {
        k: v for k, v in unscoped.items() if k != "agent"
    }


def test_outbound_dry_run_announces_the_unscoped_default(tmp_path, capsys):
    """``agent`` is IMMUTABLE, so the dry run is the last cheap moment to
    notice a forgotten ``--agent``. It must SAY which scope it will use."""
    # Arrange
    db = _outbound_sqlite(tmp_path)
    module = _load(OUTBOUND)
    # Act
    with _argv(["migrate"]):
        module.main(["--db-path", str(db)])
    # Assert
    assert "agent='' — UNSCOPED" in capsys.readouterr().out


def test_outbound_dry_run_announces_the_agent_it_was_given(tmp_path, capsys):
    """NEGATIVE CONTROL for the line above — a script that printed the
    unscoped notice unconditionally would pass that test and mislead every
    shard sweep."""
    # Arrange
    db = _outbound_sqlite(tmp_path)
    module = _load(OUTBOUND)
    # Act
    with _argv(["migrate"]):
        module.main(["--db-path", str(db), "--agent", "beta"])
    # Assert
    assert "agent='beta'" in capsys.readouterr().out
