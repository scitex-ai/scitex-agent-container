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
script did not follow it, so this pins the property for BOTH rather than fixing
one and leaving the next author to guess.

HOW IT DETECTS A WRITE, WITHOUT MOCKS. It points the store at a DSN that cannot
be reached and gives the script real rows to move. Then:

    bare invocation  -> returns 0, announces a dry run   (never opened the store)
    --commit         -> raises                           (it tried; the DSN is dead)

The unreachable DSN IS the instrument: a script that writes cannot stay silent
against it, and a script that dry-runs cannot fail against it. Both directions
are asserted, because a test that only checked the safe case would still pass if
the script were changed to write nothing ever.

LIVES IN tests/develop/, NOT the mirror tree. tests/<pkg>/ asserts that a
matching src/<pkg>/.../X.py exists; these scripts live at the REPO ROOT under
scripts/ and have no src counterpart, so a test_*.py under tests/<pkg>/ is an
orphan and PS-204 §2 fails the build. Same trap as PR #1026's
_helpers/test_ports.py -- the directory is blessed, so nothing warns until CI.

NO MONKEYPATCH (PA-306 §3 forbids mock fixtures). Env and argv are saved and
restored directly, the same shape as `env_save_restore` in
tests/scitex_agent_container/_helpers/subprocess_shim.py and the
`slurm_state_env` fixture in tests/integration/test_render_cli.py.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

#: A port nothing listens on. The scripts must fail against it if they write.
DEAD_DSN = "postgresql://nobody@127.0.0.1:59999/nothing"


@contextlib.contextmanager
def _dead_store_and_argv(argv: list[str]):
    """Point the store at an unreachable DSN and set argv, then restore both.

    PA-306: replaces ``monkeypatch.setenv`` / ``monkeypatch.setattr`` with plain
    save/restore of real state.
    """
    saved_dsn = os.environ.get("SCITEX_STORE_DSN")
    saved_argv = list(sys.argv)
    os.environ["SCITEX_STORE_DSN"] = DEAD_DSN
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = saved_argv
        if saved_dsn is None:
            os.environ.pop("SCITEX_STORE_DSN", None)
        else:
            os.environ["SCITEX_STORE_DSN"] = saved_dsn


def _load(name: str):
    path = SCRIPTS / name
    if not path.exists():
        pytest.skip(f"{name} not present in this checkout")
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@dataclass(frozen=True)
class Run:
    """What one invocation of a migration script did."""

    rc: int | None
    out: str
    error: BaseException | None


def _run_incarnations(tmp_path, capsys, extra: list[str]) -> Run:
    db = _sqlite_with_one_incarnation(tmp_path)
    module = _load("migrate_incarnations_to_postgres.py")
    argv = ["migrate", "--db", str(db), *extra]
    rc: int | None = None
    err: BaseException | None = None
    with _dead_store_and_argv(argv):
        try:
            rc = module.main()
        except BaseException as exc:
            err = exc
    return Run(rc=rc, out=capsys.readouterr().out, error=err)


def test_incarnations_bare_invocation_succeeds(tmp_path, capsys):
    """A dry run is a success, not an error."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_incarnations(target, capsys, [])
    # Assert
    assert run.rc == 0


def test_incarnations_bare_invocation_announces_a_dry_run(tmp_path, capsys):
    """It must SAY it wrote nothing, so a reader is not left guessing."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_incarnations(target, capsys, [])
    # Assert
    assert "writing nothing" in run.out


def test_incarnations_bare_invocation_never_reaches_the_store(tmp_path, capsys):
    """The dead DSN is the detector: a writer could not have stayed silent."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_incarnations(target, capsys, [])
    # Assert
    assert run.error is None


def test_incarnations_commit_does_reach_for_the_store(tmp_path, capsys):
    """NEGATIVE CONTROL — without this, the tests above would still pass if the
    script were broken to never write at all. A guard that cannot fail is not a
    guard."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_incarnations(target, capsys, ["--commit"])
    # Assert
    assert run.error is not None


def test_verdict_delivered_bare_invocation_succeeds(tmp_path):
    """The sibling already had the safe shape; pin it so it stays that way."""
    # Arrange
    module = _load("migrate_verdict_delivered_to_postgres.py")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    # Act
    with _dead_store_and_argv(["migrate"]):
        rc = module.main(["--state-db", str(empty)])
    # Assert
    assert rc == 0


# ---------------------------------------------------------------------------
# node_comms_policy — the table where a silent no-op is a PRIVILEGE change
#
# This script had no coverage here at all, which is the shape that let a
# sibling script go days unable to write a single row: its dry run returned
# before the write path, so the default invocation looked healthy and only
# ``--commit`` was broken. On THIS table the cost is worse than a lost
# observation. The script's own docstring spells it out: a missing row makes
# read_comms_policy return the all-allow defaults, so a capsule authored
# ``inbound.siblings=deny`` becomes reachable with nothing logged, and the
# same missing row strips the agent of its named groups.
# ---------------------------------------------------------------------------


def _sqlite_with_one_policy(tmp_path) -> Path:
    """One policy row, with every column the ALTER-TABLE history produced."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE node_comms_policy (
               name TEXT PRIMARY KEY, outbound_siblings TEXT NOT NULL,
               outbound_parent TEXT NOT NULL, inbound_siblings TEXT NOT NULL,
               inbound_parent TEXT NOT NULL, lineage_group TEXT NOT NULL,
               may_spawn INTEGER NOT NULL, group_name TEXT NOT NULL,
               updated_at REAL NOT NULL, group_names TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO node_comms_policy VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("agent-x", "allow", "allow", "deny", "allow", "", 1,
         "developer", 1756339200.0, "developer"),
    )
    conn.commit()
    conn.close()
    return db


def _run_node_comms_policy(tmp_path, capsys, extra: list[str]) -> Run:
    db = _sqlite_with_one_policy(tmp_path)
    module = _load("migrate_node_comms_policy_to_postgres.py")
    argv = ["migrate", "--db-path", str(db), *extra]
    rc: int | None = None
    err: BaseException | None = None
    with _dead_store_and_argv(argv):
        try:
            rc = module.main()
        except BaseException as exc:
            err = exc
    return Run(rc=rc, out=capsys.readouterr().out, error=err)


def test_node_comms_policy_bare_invocation_succeeds(tmp_path, capsys):
    """A dry run is a success, not an error."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_node_comms_policy(target, capsys, [])
    # Assert
    assert run.rc == 0


def test_node_comms_policy_bare_invocation_never_reaches_the_store(tmp_path, capsys):
    """The dead DSN is the detector: a writer could not have stayed silent."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_node_comms_policy(target, capsys, [])
    # Assert
    assert run.error is None


def test_node_comms_policy_commit_does_reach_for_the_store(tmp_path, capsys):
    """NEGATIVE CONTROL — the one that matters.

    Without it the two tests above still pass on a script that can never
    write anything, which is precisely how the diary migration shipped
    unable to carry a single row. Reaching an unreachable DSN must raise.
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_node_comms_policy(target, capsys, ["--commit"])
    # Assert
    assert run.error is not None
