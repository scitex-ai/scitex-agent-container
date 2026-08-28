#!/usr/bin/env python3
"""The migration CREATES its tables, so it decides who can use them.

THE OUTAGE THIS PINS (measured on the fleet, 2026-08-28)
========================================================
The one-shot was run as ``ywatanabe__cli``. ``sac_channel_events`` and
``sac_channel_cursor`` came out owned by that leaf role with a NULL ``relacl``
— no grants to anybody. Every agent connects as ``ywatanabe__<agent>`` and
opens the channel through ``init_channel_schema``, whose
``CREATE INDEX IF NOT EXISTS`` requires OWNERSHIP of the table rather than
privileges on it. The fleet's message channel began failing with
``InsufficientPrivilege: must be owner of table sac_channel_events`` three
minutes later and stayed broken for six, until the tables were reowned to
``scitex_store_owner`` by break-glass.

The post-migration verification PASSED for the whole outage, because it ran as
the MIGRATING role. That is the specific trap these tests are built around:
every assertion below is about a role that is NOT ``current_user``.

WHAT MAKES THESE TESTS REAL. They need two roles in the cluster — one that
owns the schema's other tables, and one that inherits it — and they DERIVE
that pair from the catalog rather than naming ``scitex_store_owner``, which is
a fact about this fleet and not about the code. Where no such pair exists the
module SKIPS with the reason spelled out, because a skip that looks like a
pass is how a suite comes to report green while executing nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state.state_db_channel_store import (
    reset_channel_connection,
)
from tests.develop._channel_migration_kit import (
    legacy_db,
    query,
    raw_conn,
    raw_query,
    run,
    script_module,
)
from tests.develop._channel_migration_kit import seed_rows as _seed_rows

MANAGED = ("sac_channel_events", "sac_channel_cursor", "sac_channel_import")

#: A name no cluster has. Used to pin the refusal, and portable because its
#: absence is the property under test rather than an assumption about roles.
ABSENT_ROLE = "sac_no_such_owner_role_20260828"


@pytest.fixture(autouse=True)
def _drop_cached_connection() -> Iterator[None]:
    reset_channel_connection()
    yield
    reset_channel_connection()


@pytest.fixture()
def store_owner(pg_schema: str) -> str:
    """A role this session can hand a table to, that OTHER logins inherit.

    Derived, never named: the pair is whatever the cluster actually has. On
    the fleet this resolves into the ``ywatanabe__<agent>`` -> ``ywatanabe``
    -> ``scitex_store_owner`` chain measured on the primary; on a throwaway
    database it resolves to whatever equivalent that database was given.

    """
    with raw_conn() as conn:
        row = conn.execute(
            "SELECT o.rolname, COUNT(*) FROM pg_roles o CROSS JOIN pg_roles r "
            "WHERE pg_has_role(current_user, o.oid, 'USAGE') "
            "AND o.rolname <> current_user "
            "AND r.rolcanlogin AND NOT r.rolsuper AND r.rolname <> current_user "
            "AND pg_has_role(r.oid, o.oid, 'USAGE') "
            "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1"
        ).fetchone()
        if row is None:
            pytest.skip(
                "this cluster has no role this session can hand a table to "
                "that another login role inherits, so the 2026-08-28 "
                "ownership shape cannot be reproduced here"
            )
        owner = str(row[0])
    return owner


@pytest.fixture()
def store_reference(store_owner: str) -> str:
    """A table in the schema owned by ``store_owner`` — the rest of the store.

    This is what the migration derives its intended owner from, and what the
    consumer check derives its population from. Both questions are then about
    a table the migration did NOT create, which is the only way either answer
    can be independent of the thing being tested.
    """
    _add_store_reference(store_owner)
    return "store_reference"


def _add_store_reference(owner: str) -> None:
    """Put a table owned by ``owner`` into the schema under test.

    USAGE as well as CREATE: without USAGE the role cannot even see the
    schema, and ``CREATE TABLE`` fails with "no schema has been selected to
    create in" rather than with a permission error — a message that names the
    wrong problem. The fleet's ``public`` schema grants both already.
    """
    from psycopg import sql

    with raw_conn() as conn:
        schema = str(conn.execute("SELECT current_schema()").fetchone()[0])
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(owner)
            )
        )
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(owner)))
        conn.execute(
            sql.SQL("CREATE TABLE {}.store_reference (id BIGINT PRIMARY KEY)").format(
                sql.Identifier(schema)
            )
        )
        conn.execute("RESET ROLE")


def _owner_of(table: str) -> str | None:
    rows = raw_query(
        "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s",
        (table,),
    )
    return None if not rows else str(rows[0][0])


def test_the_tables_land_owned_by_the_role_the_rest_of_the_schema_uses(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """The intended owner is DERIVED from the schema, and applied."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit")
    # Assert
    assert _owner_of("sac_channel_events") == store_owner


def test_the_cursor_table_lands_owned_by_it_too(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """``sac_channel_cursor`` is on the same publish path and breaks the same way."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit")
    # Assert
    assert _owner_of("sac_channel_cursor") == store_owner


def test_the_provenance_ledger_lands_owned_by_it_too(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """A table this script creates is a table a later run must be able to use."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit")
    # Assert
    assert _owner_of("sac_channel_import") == store_owner


def test_a_consumer_role_can_act_as_the_owner_afterwards(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """THE QUESTION THAT WAS NEVER ASKED ON 2026-08-28.

    ``pg_has_role(<consumer>, <table owner>, 'USAGE')`` IS PostgreSQL's own
    ownership test, and it is what ``CREATE INDEX IF NOT EXISTS`` consults
    every time an agent opens the channel. Asked about a role that is not
    ``current_user``, so the writer's own access cannot answer it.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    run(db, "--commit")
    consumers = [
        str(r[0])
        for r in raw_query(
            "SELECT rolname FROM pg_roles WHERE rolcanlogin AND NOT rolsuper "
            "AND rolname <> current_user AND pg_has_role(rolname, %s, 'USAGE') "
            "ORDER BY 1",
            (store_owner,),
        )
    ]
    # Act
    verdicts = raw_query(
        "SELECT bool_and(pg_has_role(r.rolname, "
        "(SELECT c.relowner FROM pg_class c WHERE c.oid = "
        "'sac_channel_events'::regclass), 'USAGE')) FROM pg_roles r "
        "WHERE r.rolname = ANY(%s)",
        (consumers,),
    )
    # Assert
    assert verdicts == [(True,)]


def test_a_table_created_by_the_wrong_role_is_handed_back(
    tmp_path: Path, pg_schema: str, store_owner: str
) -> None:
    """THE REPAIR PATH — the state every host left by the old script is in.

    The first run has no reference table to learn from, so it creates the
    tables under the migrating role: precisely what happened as
    ``ywatanabe__cli``. The reference table then appears, and the second run
    must hand them over rather than leaving them stranded.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    run(db, "--commit")
    _add_store_reference(store_owner)
    # Act
    run(db, "--commit")
    # Assert
    assert _owner_of("sac_channel_events") == store_owner


def test_the_repair_does_not_disturb_the_rows(
    tmp_path: Path, pg_schema: str, store_owner: str
) -> None:
    """Reowning is a catalog change; the ids a consumer holds must not move."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    run(db, "--commit")
    _add_store_reference(store_owner)
    # Act
    run(db, "--commit")
    # Assert
    assert query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,)]


def test_an_unreachable_owner_refuses_before_creating_anything(
    tmp_path: Path, pg_schema: str
) -> None:
    """Failing HERE beats a channel outage three minutes later.

    A named owner that does not exist is the portable form of "this session
    cannot leave the tables usable": the refusal has to fire before the DDL,
    not after the rows are in.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    rc = run(db, "--commit", "--table-owner", ABSENT_ROLE)
    # Assert
    assert rc == 1


def test_naming_an_owner_the_fleet_cannot_inherit_refuses(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """2026-08-28's shape, requested EXPLICITLY, must still be refused.

    ``--table-owner`` naming this session's own role passes the "can I hand a
    table to it" test trivially — it is already ours — and reproduces the
    outage exactly. Only a question about OTHER roles catches it.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    me = str(raw_query("SELECT current_user")[0][0])
    # Act
    rc = run(db, "--commit", "--table-owner", me)
    # Assert
    assert rc == 1


def test_that_refusal_creates_no_tables_at_all(
    tmp_path: Path, pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """THE GATE IS BEFORE THE DDL, and this is what proves it.

    A check that ran only after the tables existed would refuse while leaving
    behind precisely the unusable tables it was refusing — a guard that
    creates the hazard it reports.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    me = str(raw_query("SELECT current_user")[0][0])
    # Act
    run(db, "--commit", "--table-owner", me)
    # Assert
    assert raw_query("SELECT to_regclass('sac_channel_events') IS NULL") == [(True,)]


def test_that_refusal_leaves_the_store_untouched(
    tmp_path: Path, pg_schema: str
) -> None:
    """All-or-nothing covers the ownership refusal too — no half-made tables."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit", "--table-owner", ABSENT_ROLE)
    # Assert
    assert raw_query("SELECT to_regclass('sac_channel_events') IS NULL") == [(True,)]


def test_the_consumer_check_catches_a_leaf_owned_table(
    pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """THE INSTRUMENT ITSELF, pointed at 2026-08-28's exact shape.

    A managed table owned by the migrating LEAF role, in a schema whose other
    table belongs to the store owner. The writer can use it — that is what the
    night's verification measured — and the roles derived from the reference
    table cannot. The check must say so.
    """
    # Arrange
    owners = script_module("_pg_table_owner")
    with raw_conn() as conn:
        conn.execute("CREATE TABLE sac_channel_events (id BIGINT PRIMARY KEY)")
        # Act
        problems, _note = owners.consumer_access_problems(conn, managed=MANAGED)
    # Assert
    assert problems


def test_the_consumer_check_passes_once_the_table_is_reowned(
    pg_schema: str, store_owner: str, store_reference: str
) -> None:
    """NEGATIVE CONTROL — the same instrument must go quiet when it should.

    A check that complained either way would pass the test above while telling
    an operator nothing.
    """
    # Arrange
    from psycopg import sql

    owners = script_module("_pg_table_owner")
    with raw_conn() as conn:
        conn.execute("CREATE TABLE sac_channel_events (id BIGINT PRIMARY KEY)")
        conn.execute(
            sql.SQL("ALTER TABLE sac_channel_events OWNER TO {}").format(
                sql.Identifier(store_owner)
            )
        )
        # Act
        problems, _note = owners.consumer_access_problems(conn, managed=MANAGED)
    # Assert
    assert problems == []


def test_an_empty_schema_reports_the_check_as_unrun(pg_schema: str) -> None:
    """A population of nobody is not a pass, and must not read like one.

    ``pg_schema`` gives a schema holding only what the migration creates, so
    there is no reference table and therefore no consumer population. Saying
    "unchecked" is the only honest answer available.
    """
    # Arrange
    owners = script_module("_pg_table_owner")
    # Act
    with raw_conn() as conn:
        _problems, note = owners.consumer_access_problems(conn, managed=MANAGED)
    # Assert
    assert "UNCHECKED" in note
