"""``incarnations`` — the birth certificate, on a REAL PostgreSQL.

Mirrors ``src/scitex_agent_container/_state/state_db_incarnations.py``.

One record joins the three settled identities (spec / agent / incarnation)
plus the compiled-spec snapshot at launch; the exit mirror completes the
life-and-death record.

WHY THESE TESTS TALK TO A REAL DATABASE
=======================================
The module under test is PostgreSQL-only by the operator's 2026-08-19 order
("fail fast, fail loud, no fallbacks"), and it reaches PostgreSQL through
``scitex_dev.store``. A suite that exercised the store's SQLite dialect
instead would be testing a code path production can never take — the exact
defect scitex-dev 0.49.0 shipped, where the PostgreSQL backend could be
WRITTEN to and never READ from.

AND WHY THEY MUST NOT SKIP
==========================
There is deliberately NO ``skipif`` on database availability. A skip that
reads as a pass is the defect this migration exists to remove (same shape as
the ``importorskip`` bug fixed in #1108). Every runner in the gate pool has a
PostgreSQL on 127.0.0.1:55432, so an unreachable store is a broken fleet,
not a test-environment quirk, and the red is correct.

ISOLATION WITHOUT PRIVILEGE
===========================
Each test gets its own PostgreSQL SCHEMA inside the existing ``scitex``
database, selected through ``search_path`` in the DSN, and dropped with
``CASCADE`` afterwards — a schema and not a database because creating a
database needs ``CREATEDB`` and the fleet is not uniform there (compute-03's
``scitex_cards`` role has ``rolcreatedb=False``), which would make the suite
pass on three runners and fail on the fourth.

Real ``os.environ`` save/restore, not ``monkeypatch`` — PA-306 forbids mocks,
and the point is that the REAL resolver reads the REAL variable.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import psycopg
import pytest

from scitex_agent_container._state.state_db_incarnations import (
    STORE_NAME,
    get_incarnation,
    incarnation_store_target,
    init_incarnations_schema,
    record_incarnation_birth,
    record_incarnation_exit,
)

#: The per-host store. Loopback only — every fleet PostgreSQL refuses
#: non-local connections at pg_hba, measured 2026-08-19.
_BASE_DSN = os.environ.get(
    "SAC_TEST_PG_DSN", "postgresql://scitex_cards@127.0.0.1:55432/scitex"
)


@pytest.fixture()
def pg_schema() -> Iterator[str]:
    """A throwaway PostgreSQL schema, wired in via ``SCITEX_STORE_DSN``.

    Yields the schema name. Anything the module writes lands here and is
    dropped afterwards, so the live birth-certificate store is never
    touched.
    """
    schema = "sac_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    key = "SCITEX_STORE_DSN"
    saved = os.environ.get(key)
    os.environ[key] = f"{_BASE_DSN}?options=-csearch_path%3D{schema}"
    try:
        yield schema
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _tables_in(schema: str) -> set[str]:
    """The table names PostgreSQL actually holds in ``schema``."""
    with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchall()
    return {r[0] for r in rows}


def _birth(incarnation: str = "inc-1", *, sha: str = "abc123") -> str:
    return record_incarnation_birth(
        incarnation,
        agent_id="alpha",
        spec_id="/specs/alpha/spec.yaml",
        spec_git_sha=sha,
        host="h1",
        compiled_spec_json='{"name": "alpha"}',
    )


# ----------------------------------------------------------------------
# The store exists, and it is PostgreSQL.
# ----------------------------------------------------------------------


def test_init_creates_the_incarnations_store_in_postgres(pg_schema: str) -> None:
    """POSITIVE CONTROL for every test below.

    Read through a SECOND, INDEPENDENT client (raw psycopg, plain SQL): if
    this passes, the rows the other tests assert on are demonstrably in
    PostgreSQL and not in some in-process stand-in. Without it, a suite that
    silently wrote nowhere would still be green.
    """
    # Arrange: the fixture created an empty schema and pointed the resolver
    # at it. Captured so the assertion below reads a TRANSITION rather than
    # a snapshot — a table that had always been there would pass a bare
    # membership check without this module having done anything.
    before = _tables_in(pg_schema)
    # Act
    init_incarnations_schema()
    # Assert
    assert f"{STORE_NAME}_rows" in _tables_in(pg_schema) - before


def test_init_returns_a_locator_naming_the_postgres_endpoint(pg_schema: str) -> None:
    """The return value names WHERE the state went, so it can be checked.

    It names the DATABASE and not the ``search_path`` schema layered on top
    — measured 2026-08-19, when this test was first written asserting the
    schema and failed with ``'sac_test_...' in 'postgres://127.0.0.1:55432/
    scitex'``. Pinned as discovered rather than quietly weakened: an
    operator following this string reaches the right server, and still has
    to know which schema to look in.

    ASSERTS ON THE PARTS, NOT THE PUNCTUATION, and that is the fix for how
    this test broke. The quotation in the paragraph above is the locator as
    scitex-dev 0.56.0 rendered it — ``postgres://127.0.0.1:55432/scitex`` —
    and the old assertion spliced the tail off ``_BASE_DSN`` and looked for
    that substring verbatim. 0.56.1 renders the same endpoint as
    ``postgres[host=127.0.0.1 db=scitex port=55432]``.

    Nothing about where the state goes changed. A developer machine pinned
    to 0.56.0 stayed green while CI, which resolves the newest, went red —
    so this was invisible to every local run and only a full CI
    reproduction found it.

    The locator is a REDACTED DISPLAY FORM, deliberately: the real
    connection string is ``target.dsn``. A display form is not a contract
    and must not be asserted on as if it were. What this test means is "the
    locator names this host, this port and this database", which survives
    any reformatting that still names them — including both forms above.
    """
    # Arrange: the endpoint the fixture routed this test's writes through.
    host_port, _, database = _BASE_DSN.split("@", 1)[-1].partition("/")
    host, _, port = host_port.partition(":")
    # Act
    locator = init_incarnations_schema()
    # Assert
    assert all(part in locator for part in (host, port, database))


# ----------------------------------------------------------------------
# Birth
# ----------------------------------------------------------------------


def test_birth_record_is_readable_by_incarnation_id(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    row = get_incarnation("inc-1")
    # Assert
    assert row is not None and row["agent_id"] == "alpha"


def test_birth_record_carries_the_spec_git_sha(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    row = get_incarnation("inc-1")
    # Assert
    assert row["spec_git_sha"] == "abc123"


def test_birth_record_carries_the_compiled_spec_json(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    row = get_incarnation("inc-1")
    # Assert
    assert row["compiled_spec_json"] == '{"name": "alpha"}'


def test_birth_is_upsert_on_the_incarnation_key(pg_schema: str) -> None:
    # Arrange: a retried launch re-records the same incarnation.
    _birth()
    _birth(sha="def456")
    # Act
    row = get_incarnation("inc-1")
    # Assert: refreshed, not duplicated / not crashed.
    assert row["spec_git_sha"] == "def456"


def test_birth_returns_the_incarnation_id(pg_schema: str) -> None:
    # Arrange
    incarnation = "inc-returns"
    # Act
    returned = _birth(incarnation)
    # Assert
    assert returned == incarnation


# ----------------------------------------------------------------------
# Death
# ----------------------------------------------------------------------


def test_exit_mirror_updates_the_birth_record(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    record_incarnation_exit("inc-1", reason="harness-returned", code=1)
    row = get_incarnation("inc-1")
    # Assert
    assert (row["exit_reason"], row["exit_code"]) == ("harness-returned", 1)


def test_exit_mirror_stamps_exited_at(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    record_incarnation_exit("inc-1", reason="stopped-by-signal", code=0)
    row = get_incarnation("inc-1")
    # Assert
    assert row["exited_at"] is not None


def test_exit_does_not_hollow_out_the_birth_fields(pg_schema: str) -> None:
    """The exit write must not blank the certificate it is completing.

    The store has no UPDATE-only verb, so the exit path READS the record and
    writes the whole thing back. If that ever collapses into a bare ``put``
    of the three exit fields, the birth data would depend on the store's
    per-field merge to survive — this asserts it survives outright.
    """
    # Arrange
    _birth()
    # Act
    record_incarnation_exit("inc-1", reason="harness-returned", code=1)
    row = get_incarnation("inc-1")
    # Assert
    assert row["compiled_spec_json"] == '{"name": "alpha"}'


def test_exit_without_birth_reports_false_not_insert(pg_schema: str) -> None:
    # Arrange: no birth record for this id.
    _birth("inc-other")
    # Act
    updated = record_incarnation_exit("inc-ghost", reason="crashed", code=1)
    # Assert: a death with no recorded birth is a real signal, not an
    # excuse to fabricate one.
    assert updated is False


def test_exit_without_birth_writes_nothing(pg_schema: str) -> None:
    """The False above must mean "wrote nothing", not "wrote and said no"."""
    # Arrange
    _birth("inc-other")
    # Act
    record_incarnation_exit("inc-ghost", reason="crashed", code=1)
    # Assert
    assert get_incarnation("inc-ghost") is None


def test_last_exit_write_wins(pg_schema: str) -> None:
    # Arrange: exit.json semantics — the LAST write is the record.
    _birth()
    record_incarnation_exit("inc-1", reason="first", code=1)
    # Act
    record_incarnation_exit("inc-1", reason="second", code=2)
    row = get_incarnation("inc-1")
    # Assert
    assert (row["exit_reason"], row["exit_code"]) == ("second", 2)


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_unknown_incarnation_reads_none(pg_schema: str) -> None:
    # Arrange
    _birth()
    # Act
    row = get_incarnation("inc-nope")
    # Assert
    assert row is None


def test_returned_dict_carries_no_store_bookkeeping(pg_schema: str) -> None:
    """Callers see the schema fields, the same shape ``sqlite3.Row`` gave.

    The store hangs hlc / seq / origin / owner off sibling attributes rather
    than the values mapping, and this pins that: a caller iterating the dict
    must not suddenly meet store internals it never asked for.
    """
    # Arrange
    _birth()
    # Act
    row = get_incarnation("inc-1")
    # Assert
    assert set(row) == {
        "incarnation_id",
        "agent_id",
        "spec_id",
        "spec_git_sha",
        "host",
        "born_at",
        "compiled_spec_json",
        "exit_reason",
        "exit_code",
        "exited_at",
    }


# ----------------------------------------------------------------------
# The SQLite table is gone, and asking for it must SAY so.
# ----------------------------------------------------------------------


def test_incarnations_is_no_longer_a_sqlite_known_table() -> None:
    """Inverted on 2026-08-19, and the inversion is the point.

    While ``incarnations`` stayed whitelisted, ``sac db query
    --table=incarnations`` would open state.db, find no such table, and
    return an EMPTY result — which reads as "this agent has no
    incarnations" when the truth is "you are asking the wrong database".
    Removing the name turns that silent lie into an unknown-table error.
    """
    # Arrange
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Act
    known = set(KNOWN_TABLES)
    # Assert
    assert "incarnations" not in known


# ----------------------------------------------------------------------
# A test must not be able to write into the live fleet store
# ----------------------------------------------------------------------
#
# REGRESSION GUARD for a measured incident, 2026-08-20. PR #1154 dropped
# `db_path` from `write_birth_certificate`; tests that had been threading a
# tmp_path SQLite file silently began resolving the FLEET DSN, and one
# full-suite run on scitex-compute-04 wrote 46 rows into the live
# `incarnations` store — alpha, zombie, born-1..4, pid-*, rec-*, grant-*,
# screen-*, every one a fixture name. Removed afterwards with the store's
# own `hide` verb.
#
# The rows were the small half. sac's CI runs on SELF-HOSTED runners sitting
# on the fleet hosts, so this was a test suite scheduled to edit production
# state on every push. And it was invisible: write_birth_certificate is
# best-effort, so a test that wrote to the fleet store passed exactly like
# one that did not.
#
# THESE LIVE HERE, in the incarnations mirror, rather than in a file of
# their own: a standalone tests/scitex_agent_container/test_*.py has no src
# counterpart and the ecosystem audit rejects it (PS-204 orphan-test-file,
# measured in CI on 2026-08-20). tests/develop/ would satisfy the audit but
# is OUTSIDE the conftest whose guard these tests verify, so the guard would
# not be armed there — the tests would pass by not being covered. This is
# the table the incident polluted, so the mirror it belongs to is this one.

#: The fleet's real store. If a test ever resolves this, the guard is gone.
_FLEET_HOST_PORT = "127.0.0.1:55432"


def test_the_store_dsn_is_not_the_fleet_store_during_a_test() -> None:
    # Arrange: the autouse conftest guard has already run for this test.
    dsn = os.environ.get("SCITEX_STORE_DSN", "")
    # Act
    points_at_fleet = _FLEET_HOST_PORT in dsn
    # Assert
    assert not points_at_fleet


def test_the_resolved_target_is_not_the_fleet_store() -> None:
    """The variable being set is not the same as the resolver honouring it.

    Checked separately because ``host_store`` is a two-step resolution: a
    future change that stopped reading ``SCITEX_STORE_DSN`` would leave the
    assertion above green while sending writes back to the fleet.
    """
    # Arrange
    target = incarnation_store_target()
    # Act
    locator = str(target.locator)
    # Assert
    assert _FLEET_HOST_PORT not in locator


def test_a_birth_certificate_written_during_a_test_does_not_land() -> None:
    """The behavioural assertion: the write must not reach a real store.

    Uses the best-effort launch path, which is exactly how the 46 rows were
    written — not the store API directly, because the incident came through
    a caller that swallows failures rather than through a deliberate write.

    ASSERTS ON THE RETURN VALUE, and the first draft did not. It asserted
    ``get_incarnation(...) is None``, which cannot hold: with no store to
    reach, the READ raises exactly as the write did, so the test failed
    while the guard was working perfectly. "Nothing was written" and
    "reading finds nothing" are different claims, and only the first one is
    observable when the store is deliberately absent.
    """
    # Arrange
    from scitex_agent_container._lifecycle._birth_certificate import (
        write_birth_certificate,
    )
    from scitex_agent_container.config import AgentConfig

    cfg = AgentConfig(name="guard-must-not-persist")
    # Act
    landed = write_birth_certificate(cfg, "inc-guard-must-not-persist")
    # Assert: the launch path reports the record did not land.
    assert landed is False


def test_the_guard_lets_a_test_opt_in_to_a_real_store(pg_schema: str) -> None:
    """POSITIVE CONTROL — without it the three tests above are unfalsifiable.

    A guard that blocked EVERY store access would satisfy all of them and
    also break every legitimate PostgreSQL test in the suite. This proves
    the opt-in path still reaches a real database, just not the fleet's.
    """
    # Arrange
    from scitex_agent_container._state.state_db_incarnations import (
        record_incarnation_birth,
    )

    record_incarnation_birth(
        "inc-guard-opt-in",
        agent_id="guard",
        spec_id=None,
        spec_git_sha="unresolvable",
        host="h",
        compiled_spec_json="{}",
    )
    # Act
    row = get_incarnation("inc-guard-opt-in")
    # Assert
    assert row is not None
