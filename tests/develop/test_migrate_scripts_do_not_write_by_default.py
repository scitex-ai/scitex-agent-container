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


# ---------------------------------------------------------------------------
# instances — the largest table, and the one whose verify could not be default
#
# ``_migrate_lib``'s ``run_migration`` compares the source row count against
# whatever ``verify()`` returns, and every earlier consumer let that be a
# GLOBAL count of the store. For ``instances`` that comparison cannot hold:
# the store is SHARED, so once compute-04 has migrated, spartan's run reads
# its own 200 rows and sees 800 in the store. A check whose verdict depends
# on run order is not a check, so this script's ``_verify`` counts the source
# ids it actually carried. These tests pin the same two directions as their
# neighbours — a bare run must not reach the store, and ``--commit`` must.
# ---------------------------------------------------------------------------


def _sqlite_with_one_instance(tmp_path) -> Path:
    """One row, with every column the ALTER-TABLE history produced.

    The row is ENDED deliberately: the migration must carry those, and a
    fixture holding only live rows would let a filtered source pass.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE instances (
               id TEXT PRIMARY KEY, definition_id TEXT, name TEXT NOT NULL,
               host TEXT NOT NULL, scope TEXT NOT NULL, pid INTEGER,
               ppid INTEGER, screen TEXT, workdir TEXT, a2a_port INTEGER,
               started_at TEXT NOT NULL, last_heartbeat_at TEXT,
               ended_at TEXT, exit_reason TEXT, iter_count INTEGER,
               input_tokens INTEGER, output_tokens INTEGER,
               bound_port INTEGER, remote INTEGER DEFAULT 0,
               spawned_by TEXT)"""
    )
    conn.execute(
        "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("inst-1", None, "agent-x", "test-host", "global", 4321, None,
         "sac-agent-x", "/home/u/proj/agent-x", 8001,
         "2026-08-24T00:00:00Z", None, "2026-08-25T00:00:00Z", "stopped",
         0, 0, 0, 8001, 0, "cli"),
    )
    conn.commit()
    conn.close()
    return db


@contextlib.contextmanager
def _with_verify_role(role: str):
    """Save/restore ``SAC_MIGRATE_VERIFY_ROLE`` around one call.

    Added alongside the consumer-verify refusal (the check that names WHICH
    relation it counted and refuses under ``--commit`` when this variable is
    unset): without setting it here, ``test_instances_commit_does_reach_for_
    the_store`` below would stop short at that new refusal and never reach
    the dead DSN it exists to pin — the numeric assertion would still pass,
    but for the wrong reason. PA-306: save/restore, no monkeypatch.
    """
    key = "SAC_MIGRATE_VERIFY_ROLE"
    saved = os.environ.get(key)
    os.environ[key] = role
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _run_instances(tmp_path, capsys, extra: list[str]) -> Run:
    db = _sqlite_with_one_instance(tmp_path)
    module = _load("migrate_instances_to_postgres.py")
    argv = ["migrate", "--db-path", str(db), *extra]
    rc: int | None = None
    err: BaseException | None = None
    with _with_verify_role("sac_test_consumer_20260829"), _dead_store_and_argv(argv):
        try:
            rc = module.main()
        except BaseException as exc:
            err = exc
    return Run(rc=rc, out=capsys.readouterr().out, error=err)


def test_instances_bare_invocation_succeeds(tmp_path, capsys):
    """A dry run is a success, not an error."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, [])
    # Assert
    assert run.rc == 0


def test_instances_bare_invocation_announces_a_dry_run(tmp_path, capsys):
    """It must SAY it wrote nothing, so a reader is not left guessing."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, [])
    # Assert
    assert "DRY RUN" in run.out


def test_instances_bare_invocation_lists_the_ended_row(tmp_path, capsys):
    """ENDED rows MOVE.

    ``last_known_instance``, ``_restart_verify`` and ``_reconcile/_rule`` all
    read one as evidence, and a missing record is a DIFFERENT verdict
    (NEVER_STARTED) rather than merely less detail. A source query that
    filtered them out would still print a plausible dry run.
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, [])
    # Assert
    assert "ENDED" in run.out


def test_instances_bare_invocation_never_reaches_the_store(tmp_path, capsys):
    """The dead DSN is the detector: a writer could not have stayed silent."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, [])
    # Assert
    assert run.error is None


def test_instances_commit_does_reach_for_the_store(tmp_path, capsys):
    """NEGATIVE CONTROL — without it the tests above still pass on a script
    that can never write anything, which is exactly how the diary migration
    shipped unable to carry a single row.

    It asserts a NONZERO EXIT rather than a raised exception, and the
    difference is the ownership preflight added on 2026-08-28: ``--commit``
    now connects BEFORE ``run_migration`` to check who would own the tables
    it creates, so against the dead DSN it fails there and returns 1 instead
    of raising deeper in. Either way the claim is the same and is the one
    that matters — the commit path REACHED for the store, and the bare run
    above did not.
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, ["--commit"])
    # Assert
    assert (run.rc, run.error) != (0, None)


def _run_instances_no_verify_role(tmp_path, capsys, extra: list[str]) -> Run:
    """Like :func:`_run_instances`, but with the role popped, not set.

    The env var is POPPED rather than merely left alone, so the two tests
    below fire regardless of the ambient environment they happen to run in.
    """
    db = _sqlite_with_one_instance(tmp_path)
    module = _load("migrate_instances_to_postgres.py")
    argv = ["migrate", "--db-path", str(db), *extra]
    key = "SAC_MIGRATE_VERIFY_ROLE"
    saved = os.environ.pop(key, None)
    rc: int | None = None
    err: BaseException | None = None
    try:
        with _dead_store_and_argv(argv):
            try:
                rc = module.main()
            except BaseException as exc:
                err = exc
    finally:
        if saved is not None:
            os.environ[key] = saved
    return Run(rc=rc, out=capsys.readouterr().out, error=err)


def test_instances_commit_without_verify_role_refuses(tmp_path, capsys):
    """``SAC_MIGRATE_VERIFY_ROLE`` unset must REFUSE under ``--commit``.

    THE RED THIS PINS: before this refusal existed, an unset role made the
    post-migration consumer check a WARNING that still returned success —
    "the verification above ran as the MIGRATING role ... passed while the
    fleet could not read the rows at all" (measured 2026-08-28).
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances_no_verify_role(target, capsys, ["--commit"])
    # Assert
    assert run.rc == 1


def test_instances_commit_without_verify_role_names_the_missing_variable(
    tmp_path, capsys
):
    """The refusal names ITS OWN variable, distinct from the ownership one.

    So a reader — or a second assertion here, split out per STX-TQ007 — can
    tell this refusal apart from ``_preflight_ownership``'s.
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances_no_verify_role(target, capsys, ["--commit"])
    # Assert
    assert "SAC_MIGRATE_VERIFY_ROLE" in run.out


def test_instances_dry_run_ignores_a_missing_verify_role(tmp_path, capsys):
    """NEGATIVE CONTROL — the refusal is ``--commit``-only, per the brief.

    Without this, a change that made the refusal fire unconditionally would
    still pass every test above (they only ever call the commit form).
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances_no_verify_role(target, capsys, [])
    # Assert
    assert run.rc == 0


# ---------------------------------------------------------------------------
# inbound_dispatches — THE MIGRATION THAT DID NOT EXIST
#
# THIS FILE ENUMERATES SCRIPTS BY HAND. There is no discovery loop over
# ``scripts/migrate_*.py``, so a new migration is covered by this guard only
# when somebody adds a section like this one. That is the same shape as the
# gap being closed here: the cutover (#1169, 2026-08-20) landed with no
# migration script at all, and nothing in the repo enumerated the thirteen
# tables that had one against the fourteen that had moved.
#
# The table matters more than most. Measured fleet-wide 2026-08-29: 5,200+
# rows stranded in SQLite, 133 of them still ``pending``/``reporting`` — each
# an inbound dispatch whose requester is still owed a completion report from
# a ``claim_oldest_pending`` that reads PostgreSQL and nothing else. So a
# bare invocation that quietly wrote, or a ``--commit`` that quietly could
# not, are both worse here than a lost observation.
# ---------------------------------------------------------------------------


def _sqlite_with_inbound_dispatches(tmp_path) -> Path:
    """One settled row and one unfinished one, with the DDL #1169 deleted.

    The ``pending`` row is deliberate: a source query that filtered
    unfinished dispatches out would still print a plausible dry run, and
    those are the rows the migration exists for.
    """
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
        [
            ("agent-x", "lead", "d-1", "reported", 1000.5, 1001.0),
            ("agent-x", "lead", None, "pending", 1002.5, None),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _run_inbound_dispatches(tmp_path, capsys, extra: list[str]) -> Run:
    db = _sqlite_with_inbound_dispatches(tmp_path)
    module = _load("migrate_inbound_dispatches_to_postgres.py")
    argv = ["migrate", "--db-path", str(db), *extra]
    rc: int | None = None
    err: BaseException | None = None
    with _dead_store_and_argv(argv):
        try:
            rc = module.main()
        except BaseException as exc:
            err = exc
    return Run(rc=rc, out=capsys.readouterr().out, error=err)


def test_inbound_dispatches_bare_invocation_succeeds(tmp_path, capsys):
    """A dry run is a success, not an error."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_inbound_dispatches(target, capsys, [])
    # Assert
    assert run.rc == 0


def test_inbound_dispatches_bare_invocation_announces_a_dry_run(tmp_path, capsys):
    """It must SAY it wrote nothing, so a reader is not left guessing."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_inbound_dispatches(target, capsys, [])
    # Assert
    assert "DRY RUN" in run.out


def test_inbound_dispatches_bare_invocation_lists_the_unfinished_row(
    tmp_path, capsys
):
    """UNFINISHED rows MOVE, and are marked.

    A ``pending`` row is a completion report still owed; a source query that
    carried only settled history would still print a plausible dry run, and
    the debt would stay invisible to the only code that can discharge it.
    """
    # Arrange
    target = tmp_path
    # Act
    run = _run_inbound_dispatches(target, capsys, [])
    # Assert
    assert "UNFINISHED" in run.out


def test_inbound_dispatches_bare_invocation_never_reaches_the_store(
    tmp_path, capsys
):
    """The dead DSN is the detector: a writer could not have stayed silent."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_inbound_dispatches(target, capsys, [])
    # Assert
    assert run.error is None


def test_inbound_dispatches_commit_does_reach_for_the_store(tmp_path, capsys):
    """NEGATIVE CONTROL — without it the three tests above still pass on a
    script that can never write anything, which is exactly how the diary
    migration shipped unable to carry a single row."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_inbound_dispatches(target, capsys, ["--commit"])
    # Assert
    assert run.error is not None
