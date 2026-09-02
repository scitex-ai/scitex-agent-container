"""The bot-token ownership ledger — on a REAL PostgreSQL, never a local file, never a mock.

Mirrors ``src/scitex_agent_container/_state/state_db_token_owner.py``.

WHY THESE TESTS TALK TO A REAL DATABASE
=======================================
The module under test is PostgreSQL-only by the operator's 2026-08-19 order
("fail fast, fail loud, no fallbacks") and reaches PostgreSQL through
``scitex_dev.store``. A suite that exercised the store's file-backed dialect would
be testing a code path production can never take — and scitex-dev 0.49.0
shipped a PostgreSQL backend that could be WRITTEN to and never READ from,
precisely because that path had been written to and never read from. So the
rows here are read BACK through the same dialect production uses.

AND WHY THEY MUST NOT SKIP
==========================
There is deliberately NO ``skipif`` on database availability. A skip that
reads as a pass is the defect this migration exists to remove. If PostgreSQL
is unreachable these tests FAIL, and that red is correct: every runner in the
gate pool has a PostgreSQL on 127.0.0.1:55432.

THE TEST THAT CARRIES THE DESIGN
================================
``test_a_second_agent_on_the_same_token_does_not_overwrite_the_first``. The
obvious key for this ledger is ``token_fp`` alone, and that key destroys the
evidence: the second claimant overwrites the first and a store whose whole
purpose is to reveal double ownership renders every collision as one tidy row.
The identity is ``(token_fp, host, agent)`` so a contended token simply holds
more than one row.

Isolation is a per-test PostgreSQL SCHEMA selected through ``search_path`` in
the DSN and dropped with ``CASCADE`` — a schema and not a database, because
creating a database needs ``CREATEDB`` and the fleet's roles are not uniform.
Real ``os.environ`` save/restore, not ``monkeypatch``: PA-306 forbids mocks,
and the point is that the REAL resolver reads the REAL variable.
"""

from __future__ import annotations

from tests._store_isolation import pg_endpoint_port

import psycopg

from scitex_agent_container._account._rotation_audit import fingerprint_token
from scitex_agent_container._state.state_db_token_owner import (
    init_token_owner_schema,
    owners_of,
    record_token_owner,
    token_owner_rows,
)

#: The per-host store, shared with every other PostgreSQL-backed test.
#:
#: Was a private copy carrying ``postgresql://scitex_cards@...``. That role is
#: the fleet's retired shared superuser (now NOLOGIN), so the literal had to go
#: from all four sites at once; importing removes the chance of a fifth copy
#: drifting. ``tests/_store_isolation.py`` owns the value and the identity.
from tests._store_isolation import PG_BASE_DSN as _BASE_DSN  # noqa: E402

# A value-shaped string that must never reach the ledger. Only its fingerprint
# may, and the module refuses anything that is not one.
_SECRET = "zz-not-a-real-bot-token-0000000000"
_FP = fingerprint_token(_SECRET) or ""
_FP_OTHER = fingerprint_token(_SECRET + "-other") or ""


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


def test_init_creates_the_ledger_in_postgres(pg_schema: str) -> None:
    """POSITIVE CONTROL for every test below.

    Read through a SECOND, INDEPENDENT client (raw psycopg, plain SQL) rather
    than through the store that created the table. Asking a library whether
    its own write landed cannot distinguish "wrote to PostgreSQL" from "wrote
    somewhere else".
    """
    # Arrange
    expected = "cct_token_owner_rows"
    # Act
    init_token_owner_schema()
    # Assert
    assert expected in _tables_in(pg_schema)


def test_init_reports_where_the_state_went(pg_schema: str) -> None:
    # Arrange
    expected_fragment = pg_endpoint_port()
    # Act
    locator = init_token_owner_schema()
    # Assert
    assert expected_fragment in locator


# ----------------------------------------------------------------------
# Recording a claim.
# ----------------------------------------------------------------------


def test_a_claim_is_recorded(pg_schema: str) -> None:
    # Arrange
    init_token_owner_schema()
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=4242, started_at=100.0
    )
    # Assert
    assert [r["agent"] for r in owners_of(_FP)] == ["zz-one"]


def test_the_recorded_row_carries_the_pid(pg_schema: str) -> None:
    # Arrange — "who holds this bot" is unanswerable without something to
    # point a `ps` at.
    init_token_owner_schema()
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=4242, started_at=100.0
    )
    # Assert
    assert owners_of(_FP)[0]["pid"] == 4242


def test_a_second_agent_on_the_same_token_does_not_overwrite_the_first(
    pg_schema: str,
) -> None:
    """THE DESIGN TEST. A ``token_fp``-only key would destroy this evidence.

    Two agents, one bot — measured three times over on the live fleet, twice
    across hosts. The ledger must hold BOTH claims, because a store that
    silently collapses them renders every collision as a single tidy row and
    reports the fault it exists to reveal as its absence.
    """
    # Arrange
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-hub", host="zz-h1", pid=1, started_at=100.0
    )
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-proj-hub", host="zz-h2", pid=2, started_at=200.0
    )
    # Assert
    assert sorted(r["agent"] for r in owners_of(_FP)) == ["zz-hub", "zz-proj-hub"]


def test_the_same_agent_restarting_refreshes_its_row(pg_schema: str) -> None:
    # Arrange — a restart moves the pid; this row is CURRENT STATE, not a
    # historical fact, so it must move with it.
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=1, started_at=100.0
    )
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=999, started_at=200.0
    )
    # Assert
    assert owners_of(_FP)[0]["pid"] == 999


def test_the_same_agent_restarting_does_not_create_a_second_row(
    pg_schema: str,
) -> None:
    # Arrange — the other half of the same contract: a refresh must not look
    # like a collision.
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=1, started_at=100.0
    )
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=999, started_at=200.0
    )
    # Assert
    assert len(owners_of(_FP)) == 1


def test_one_agent_on_two_hosts_is_two_rows(pg_schema: str) -> None:
    # Arrange — the same NAME on two hosts is two live processes and two
    # claims on the bot, which is exactly the cross-host duplicate.
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=1, started_at=100.0
    )
    # Act
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h2", pid=2, started_at=200.0
    )
    # Assert
    assert len(owners_of(_FP)) == 2


def test_claims_on_different_tokens_are_kept_apart(pg_schema: str) -> None:
    # Arrange — the negative control: if every claim landed on one key the
    # ledger would report a fleet-wide collision and mean nothing.
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-one", host="zz-h1", pid=1, started_at=100.0
    )
    # Act
    record_token_owner(
        token_fp=_FP_OTHER, agent="zz-two", host="zz-h1", pid=2, started_at=200.0
    )
    # Assert
    assert len(owners_of(_FP)) == 1


def test_the_whole_ledger_is_newest_first(pg_schema: str) -> None:
    # Arrange
    init_token_owner_schema()
    record_token_owner(
        token_fp=_FP, agent="zz-old", host="zz-h1", pid=1, started_at=100.0
    )
    record_token_owner(
        token_fp=_FP_OTHER, agent="zz-new", host="zz-h1", pid=2, started_at=900.0
    )
    # Act
    rows = token_owner_rows()
    # Assert
    assert [r["agent"] for r in rows] == ["zz-new", "zz-old"]


def test_a_brand_new_store_is_empty_not_an_error(pg_schema: str) -> None:
    # Arrange
    init_token_owner_schema()
    # Act
    rows = token_owner_rows()
    # Assert
    assert rows == []


# ----------------------------------------------------------------------
# The one thing this ledger must never hold.
# ----------------------------------------------------------------------


def _write_and_capture(token_fp: str) -> BaseException | None:
    """Attempt a write and hand back whatever it raised, or ``None``.

    Lets the refusal tests keep one assertion each while still asserting on
    the EXCEPTION TYPE rather than on the mere fact that something went wrong.
    """
    try:
        record_token_owner(
            token_fp=token_fp, agent="zz-one", host="zz-h1", pid=1, started_at=100.0
        )
    except BaseException as exc:  # noqa: BLE001 - the value under test
        return exc
    return None


def test_a_raw_token_is_refused(pg_schema: str) -> None:
    # Arrange — the guard exists because the caller supplies this argument and
    # a raw value is exactly one forgotten fingerprint_token() call away.
    init_token_owner_schema()
    # Act
    raised = _write_and_capture(_SECRET)
    # Assert
    assert isinstance(raised, ValueError)


def test_a_refused_raw_token_is_not_written(pg_schema: str) -> None:
    # Arrange — raising is not enough; the row must not exist either.
    init_token_owner_schema()
    _write_and_capture(_SECRET)
    # Act
    rows = token_owner_rows()
    # Assert
    assert rows == []


def test_an_empty_fingerprint_is_refused(pg_schema: str) -> None:
    # Arrange — fingerprint_token returns None for an empty token, and a
    # caller that passed it through would otherwise write a nameless claim
    # that silently "collides" with every other nameless one.
    init_token_owner_schema()
    # Act
    raised = _write_and_capture("")
    # Assert
    assert isinstance(raised, ValueError)
