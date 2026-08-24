#!/usr/bin/env python3
"""A migration script's BARE invocation must not write.

WHY THIS EXISTS. `migrate_incarnations_to_postgres.py` took `--dry-run` as the
opt-in, which made the bare, obvious invocation the destructive one. Measured
2026-08-24: running it with no flags on the live host wrote 242 rows. It was
harmless only because the upsert preserves `born_at` -- not because any guard
stopped it.

Its own sibling already had the safe shape. `migrate_verdict_delivered_to_postgres.py`
takes `--commit`, documented as "actually write; without it this is a dry run
that writes nothing". The safe pattern existed one file away and the later
script did not follow it, so this test pins the property for BOTH rather than
fixing one and leaving the next author to guess.

HOW IT DETECTS A WRITE, WITHOUT MOCKS. It points the store at a DSN that cannot
be reached and gives the script real rows to move. Then:

    bare invocation  -> returns 0, no exception   (it never opened the store)
    --commit         -> raises                    (it tried, and the DSN is dead)

The unreachable DSN IS the instrument: a script that writes cannot stay silent
against it, and a script that dry-runs cannot fail against it. Both directions
are asserted, because a test that only checks the safe case would still pass if
the script were changed to write nothing ever.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"

# A port nothing listens on. Chosen high and odd; the test asserts unreachability
# rather than assuming it, so a surprise listener fails loudly instead of
# silently turning this into a no-op.
DEAD_DSN = "postgresql://nobody@127.0.0.1:59999/nothing"


def _load(name: str):
    path = SCRIPTS / name
    if not path.exists():
        pytest.skip(f"{name} not present in this checkout")
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sqlite_with_one_incarnation(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE incarnations (
               incarnation_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
               spec_id TEXT, spec_git_sha TEXT NOT NULL, host TEXT NOT NULL,
               born_at TEXT NOT NULL, compiled_spec_json TEXT NOT NULL,
               exit_reason TEXT, exit_code INTEGER, exited_at TEXT)"""
    )
    conn.execute(
        "INSERT INTO incarnations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("inc-1", "agent-x", "spec-1", "deadbeef", "test-host",
         "2026-08-24T00:00:00Z", "{}", None, None, None),
    )
    conn.commit()
    conn.close()
    return db


def test_incarnations_migration_writes_nothing_without_commit(tmp_path, monkeypatch, capsys):
    """The bare invocation must be a dry run."""
    monkeypatch.setenv("SCITEX_STORE_DSN", DEAD_DSN)
    db = _sqlite_with_one_incarnation(tmp_path)
    mod = _load("migrate_incarnations_to_postgres.py")

    monkeypatch.setattr(sys, "argv", ["migrate", "--db", str(db)])
    rc = mod.main()

    assert rc == 0, "a dry run must succeed"
    out = capsys.readouterr().out
    assert "writing nothing" in out, (
        f"bare invocation did not announce a dry run. stdout:\n{out}"
    )


def test_incarnations_migration_DOES_try_to_write_with_commit(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: --commit must actually reach for the store.

    Without this, the test above would still pass if the script were broken to
    never write at all -- a guard that cannot fail is not a guard.
    """
    monkeypatch.setenv("SCITEX_STORE_DSN", DEAD_DSN)
    db = _sqlite_with_one_incarnation(tmp_path)
    mod = _load("migrate_incarnations_to_postgres.py")

    monkeypatch.setattr(sys, "argv", ["migrate", "--db", str(db), "--commit"])
    with pytest.raises(Exception) as exc:
        mod.main()
    # It must fail because it tried to REACH the store, not for some other reason.
    assert "59999" in str(exc.value) or "nothing" in str(exc.value).lower(), (
        f"--commit failed, but not in a way that shows it reached for the dead "
        f"store: {exc.value!r}"
    )


def test_verdict_delivered_migration_also_writes_nothing_without_commit(tmp_path, monkeypatch, capsys):
    """The sibling already had the safe shape; pin it so it stays that way."""
    monkeypatch.setenv("SCITEX_STORE_DSN", DEAD_DSN)
    mod = _load("migrate_verdict_delivered_to_postgres.py")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    monkeypatch.setattr(sys, "argv", ["migrate", "--state-db", str(empty)])
    rc = mod.main(["--state-db", str(empty)])
    assert rc == 0
