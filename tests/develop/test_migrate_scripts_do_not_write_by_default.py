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
# instances + events (2026-08-28). Enrolled here on arrival rather than after
# an incident: the pattern this file exists to enforce only holds if each new
# script joins the gate, and this one moves the LEASE — a stray write against
# the wrong target un-registers running agents.
# ---------------------------------------------------------------------------


def _sqlite_with_one_instance(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE instances (
               id TEXT PRIMARY KEY, definition_id TEXT, name TEXT NOT NULL,
               host TEXT NOT NULL, scope TEXT NOT NULL, pid INTEGER,
               ppid INTEGER, screen TEXT, workdir TEXT, a2a_port INTEGER,
               started_at TEXT NOT NULL, last_heartbeat_at TEXT,
               ended_at TEXT, exit_reason TEXT, iter_count INTEGER,
               input_tokens INTEGER, output_tokens INTEGER,
               bound_port INTEGER, remote INTEGER, spawned_by TEXT)"""
    )
    conn.execute(
        "INSERT INTO instances (id, name, host, scope, started_at) "
        "VALUES (?,?,?,?,?)",
        ("inst-1", "agent-x", "test-host", "global", "2026-08-24T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db


def _run_instances(tmp_path, capsys, extra: list[str]) -> Run:
    db = _sqlite_with_one_instance(tmp_path)
    module = _load("migrate_instances_to_postgres.py")
    rc: int | None = None
    err: BaseException | None = None
    with _dead_store_and_argv(["migrate"]):
        try:
            rc = module.main(["--db-path", str(db), *extra])
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


def test_instances_bare_invocation_never_reaches_the_store(tmp_path, capsys):
    """The dead DSN is the detector: a writer could not have stayed silent."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, [])
    # Assert
    assert run.error is None


def test_instances_commit_does_reach_for_the_store(tmp_path, capsys):
    """NEGATIVE CONTROL — without this, the tests above would still pass if the
    script were broken to never write at all."""
    # Arrange
    target = tmp_path
    # Act
    run = _run_instances(target, capsys, ["--commit"])
    # Assert
    assert run.error is not None
