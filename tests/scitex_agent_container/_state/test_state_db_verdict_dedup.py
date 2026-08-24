"""CI-verdict delivered-set — on a REAL PostgreSQL, never SQLite, never a mock.

Mirrors ``src/scitex_agent_container/_state/state_db_verdict_dedup.py``.

WHY THESE TESTS TALK TO A REAL DATABASE
=======================================
The module under test is PostgreSQL-only by the operator's 2026-08-19 order
("fail fast, fail loud, no fallbacks"), and it reaches PostgreSQL through
``scitex_dev.store``. A suite that exercised the store's SQLite dialect
instead would be testing a code path production can never take.

That is not a hypothetical. scitex-dev 0.49.0 shipped a PostgreSQL backend
that could be WRITTEN to and never READ from — writes bind parameters and
survived, reads addressed columns by name against a dialect that set no row
factory. It shipped precisely because the PostgreSQL path had been written
to and never read from. Any suite that proves this module works must
therefore read back through the same dialect production uses.

AND WHY THEY MUST NOT SKIP
==========================
There is deliberately NO ``skipif`` on database availability. A skip that
reads as a pass is the defect this whole migration exists to remove, and it
is the same shape as the ``importorskip`` bug fixed in #1108, where a
missing submodule turned "cannot test" into a green run. If PostgreSQL is
unreachable these tests FAIL, and that red is correct: every runner in the
gate pool (measured 2026-08-19: scitex-01..04-org-cpu-01 sit on
scitex-compute-01..04) has a PostgreSQL on 127.0.0.1:55432, so an
unreachable store is a broken fleet, not a test-environment quirk.

ISOLATION WITHOUT PRIVILEGE
===========================
Each test gets its own PostgreSQL SCHEMA inside the existing ``scitex``
database, selected through ``search_path`` in the DSN, and dropped with
``CASCADE`` afterwards. Deliberately a schema and not a database:

  * creating a database needs ``CREATEDB``, and the fleet is NOT uniform —
    compute-03's ``scitex_cards`` role has ``rolcreatedb=False``, so a
    create-a-database fixture would pass on three runners and fail on the
    fourth, which is the worst possible flake (it looks like the code).
  * the three-way python matrix can put concurrent jobs on ONE runner, so
    the name carries a uuid; two legs get two schemas and never collide.
  * ``DROP SCHEMA ... CASCADE`` leaves nothing behind, verified in this
    file's own teardown assertion rather than assumed.

Real ``os.environ`` save/restore, not ``monkeypatch`` — PA-306 forbids mocks,
and the point is that the REAL resolver reads the REAL variable.
"""

from __future__ import annotations

import psycopg

from scitex_agent_container._state.state_db_verdict_dedup import (
    failures_since_last_success,
    init_verdict_dedup_schema,
    record_verdict_delivered,
    verdict_already_delivered,
)

#: The per-host store, shared with every other PostgreSQL-backed test.
#:
#: Was a private copy carrying ``postgresql://scitex_cards@...``. That role is
#: the fleet's retired shared superuser (now NOLOGIN), so the literal had to go
#: from all four sites at once; importing removes the chance of a fifth copy
#: drifting. ``tests/_store_isolation.py`` owns the value and the identity.
from tests._store_isolation import PG_BASE_DSN as _BASE_DSN  # noqa: E402


def _tables_in(schema: str) -> set[str]:
    """The table names PostgreSQL actually holds in ``schema``."""
    with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchall()
    return {r[0] for r in rows}


# ----------------------------------------------------------------------
# The store exists, and it is PostgreSQL.
# ----------------------------------------------------------------------


def test_init_creates_the_delivered_set_in_postgres(pg_schema: str) -> None:
    """POSITIVE CONTROL for every test below.

    Read through a SECOND, INDEPENDENT client (raw psycopg, plain SQL)
    rather than through the store that created them. Asking a library
    whether its own write landed cannot distinguish "wrote to PostgreSQL"
    from "wrote somewhere else" — which is exactly how a store can look
    healthy while sharing nothing.
    """
    # Arrange
    expected = "verdict_delivered_rows"
    # Act
    init_verdict_dedup_schema()
    # Assert
    assert expected in _tables_in(pg_schema)


def test_init_reports_where_the_state_went(pg_schema: str) -> None:
    """The return value NAMES the target, so a caller can check it.

    The SQLite version returned a Path for the same reason. A store that
    cannot say where it is is a store nobody can verify.
    """
    # Arrange
    expected_fragment = "55432"
    # Act
    locator = init_verdict_dedup_schema()
    # Assert
    assert expected_fragment in locator


def test_record_creates_the_store_lazily_without_an_explicit_init(
    pg_schema: str,
) -> None:
    """Callers never have to remember to initialise first."""
    # Arrange
    expected = "verdict_delivered_rows"
    # Act
    record_verdict_delivered(
        repo="o/r", pr=1, head_sha="abc", conclusion="success", delivered_at=1.0
    )
    # Assert
    assert expected in _tables_in(pg_schema)


# ----------------------------------------------------------------------
# The dedup key.
# ----------------------------------------------------------------------


def test_a_fresh_key_is_not_delivered(pg_schema: str) -> None:
    """A miss must return False so the caller DELIVERS rather than swallows."""
    # Arrange
    key = dict(repo="o/r", pr=7, head_sha="abc", conclusion="success")
    # Act
    seen = verdict_already_delivered(**key)
    # Assert
    assert seen is False


def test_a_recorded_key_is_delivered(pg_schema: str) -> None:
    """The whole point: a re-poll must not re-deliver."""
    # Arrange
    key = dict(repo="o/r", pr=7, head_sha="abc", conclusion="success")
    record_verdict_delivered(**key, delivered_at=1.0)
    # Act
    seen = verdict_already_delivered(**key)
    # Assert
    assert seen is True


def test_a_different_conclusion_is_a_distinct_key(pg_schema: str) -> None:
    """A re-run that flips red->green on the SAME head MUST be delivered."""
    # Arrange
    record_verdict_delivered(
        repo="o/r", pr=7, head_sha="abc", conclusion="failure", delivered_at=1.0
    )
    # Act
    seen = verdict_already_delivered(
        repo="o/r", pr=7, head_sha="abc", conclusion="success"
    )
    # Assert
    assert seen is False


def test_a_different_head_sha_is_a_distinct_key(pg_schema: str) -> None:
    """A new push is a new verdict, even with the same conclusion."""
    # Arrange
    record_verdict_delivered(
        repo="o/r", pr=7, head_sha="abc", conclusion="failure", delivered_at=1.0
    )
    # Act
    seen = verdict_already_delivered(
        repo="o/r", pr=7, head_sha="def", conclusion="failure"
    )
    # Assert
    assert seen is False


def test_recording_twice_does_not_move_the_timestamp(pg_schema: str) -> None:
    """INSERT-OR-IGNORE semantics, and this is the one that would rot silently.

    ``delivered_at`` ORDERS the failure streak. If a re-poll refreshed it,
    every re-poll would quietly reorder history and the streak cap would
    stop capping — with no error anywhere. So the second write must be a
    no-op, not an upsert.
    """
    # Arrange
    key = dict(repo="o/r", pr=7, head_sha="abc", conclusion="failure")
    record_verdict_delivered(**key, dispatch_id="first", delivered_at=100.0)
    # Act
    record_verdict_delivered(**key, dispatch_id="second", delivered_at=999.0)
    # Assert
    assert _delivered_at_of(pg_schema, **key) == 100.0


def _delivered_at_of(schema: str, *, repo: str, pr: int, head_sha: str,
                     conclusion: str) -> float:
    """Read ``delivered_at`` straight out of PostgreSQL, bypassing the store."""
    with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        row = conn.execute(
            f'SELECT delivered_at FROM "{schema}".verdict_delivered_rows '
            "WHERE repo = %s AND pr = %s AND head_sha = %s AND conclusion = %s",
            (repo, pr, head_sha, conclusion),
        ).fetchone()
    return float(row[0])


# ----------------------------------------------------------------------
# The failure streak.
# ----------------------------------------------------------------------


def test_the_streak_counts_reds_for_this_pr(pg_schema: str) -> None:
    """Three reds on three pushes is a streak of three."""
    # Arrange
    for i, sha in enumerate(("a", "b", "c")):
        record_verdict_delivered(
            repo="o/r", pr=7, head_sha=sha, conclusion="failure",
            delivered_at=float(i + 1),
        )
    # Act
    streak = failures_since_last_success(repo="o/r", pr=7)
    # Assert
    assert streak == 3


def test_the_streak_resets_after_a_green(pg_schema: str) -> None:
    """red -> green -> red starts over, so a fixed PR is not capped forever."""
    # Arrange
    record_verdict_delivered(repo="o/r", pr=7, head_sha="a",
                             conclusion="failure", delivered_at=1.0)
    record_verdict_delivered(repo="o/r", pr=7, head_sha="b",
                             conclusion="success", delivered_at=2.0)
    record_verdict_delivered(repo="o/r", pr=7, head_sha="c",
                             conclusion="failure", delivered_at=3.0)
    # Act
    streak = failures_since_last_success(repo="o/r", pr=7)
    # Assert
    assert streak == 1


def test_the_streak_ignores_another_prs_reds(pg_schema: str) -> None:
    """One noisy PR must not cap a quiet one."""
    # Arrange
    record_verdict_delivered(repo="o/r", pr=8, head_sha="a",
                             conclusion="failure", delivered_at=1.0)
    record_verdict_delivered(repo="o/r", pr=8, head_sha="b",
                             conclusion="failure", delivered_at=2.0)
    record_verdict_delivered(repo="o/r", pr=7, head_sha="c",
                             conclusion="failure", delivered_at=3.0)
    # Act
    streak = failures_since_last_success(repo="o/r", pr=7)
    # Assert
    assert streak == 1


def test_the_streak_ignores_another_repos_reds(pg_schema: str) -> None:
    """PR numbers collide across repos; the repo is part of the identity."""
    # Arrange
    record_verdict_delivered(repo="o/other", pr=7, head_sha="a",
                             conclusion="failure", delivered_at=1.0)
    record_verdict_delivered(repo="o/other", pr=7, head_sha="b",
                             conclusion="failure", delivered_at=2.0)
    record_verdict_delivered(repo="o/r", pr=7, head_sha="c",
                             conclusion="failure", delivered_at=3.0)
    # Act
    streak = failures_since_last_success(repo="o/r", pr=7)
    # Assert
    assert streak == 1


def test_the_streak_is_zero_on_a_fresh_store(pg_schema: str) -> None:
    """A brand-new store must not look like a capped PR."""
    # Arrange
    expected = 0
    # Act
    streak = failures_since_last_success(repo="o/r", pr=7)
    # Assert
    assert streak == expected
