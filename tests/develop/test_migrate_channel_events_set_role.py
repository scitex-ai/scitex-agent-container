#!/usr/bin/env python3
"""INHERIT is not SET, and the ownership gate asked the wrong one.

MEASURED ON THE FLEET PRIMARY, 2026-08-29, running the migration for real
=========================================================================
The one-shot refused mid-run, after its DDL had already executed::

    REFUSED sac_channel_import is owned by 'ywatanabe__scitex-agent-container'
    and cannot be handed to 'scitex_store_owner' from this session
    (must be able to SET ROLE "scitex_store_owner").

Yet the pre-DDL gate had passed. For that same role and owner::

    pg_has_role(..., 'USAGE')  = True      <- what the gate asked
    pg_has_role(..., 'MEMBER') = True
    pg_has_role(..., 'SET')    = False     <- what ALTER TABLE OWNER needs

    scitex_store_owner <- ywatanabe          inherit=True  set=TRUE
    ywatanabe          <- ywatanabe__<agent> inherit=True  set=FALSE

PostgreSQL 16 split membership into INHERIT and SET, and SET does not transit
a ``GRANT ... WITH SET FALSE``. That one fact explains the whole 2026-08-28
incident: INHERITANCE is why every agent can read and write the channel
tables, and the MISSING SET is why reowning them needed break-glass. On that
cluster, 127 login roles inherit ``scitex_store_owner`` and exactly THREE can
``SET ROLE`` to it.

WHAT IT COST WAS THE ORDERING, NOT THE WORDING. Because the pre-check passed
for a session that could not reown, the DDL ran and the refusal fired behind
it, leaving a table owned by a role no other agent can use — the precise
hazard the gate exists to prevent. A gate that runs before the DDL only for
the sessions that pass it is not a pre-DDL gate. So the test that matters
most here is not "does it refuse" but "does it refuse having created
NOTHING".

WHY THE EXISTING SUITE DID NOT CATCH IT: the throwaway cluster granted its
role tree with PostgreSQL's DEFAULT (``SET TRUE``), so the fixture was more
permissive than production and could not reproduce production's failure. The
fixture now grants consumers ``WITH INHERIT TRUE, SET FALSE`` exactly as the
fleet does.
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
    raw_conn,
    raw_query,
    run,
    script_module,
)
from tests.develop._channel_migration_kit import seed_rows as _seed_rows

MANAGED = ("sac_channel_events", "sac_channel_cursor", "sac_channel_import")


@pytest.fixture(autouse=True)
def _drop_cached_connection() -> Iterator[None]:
    reset_channel_connection()
    yield
    reset_channel_connection()


@pytest.fixture()
def unsettable_owner(pg_schema: str) -> str:
    """A role this session INHERITS but cannot ``SET ROLE`` to.

    Derived, never named. On the fleet primary this finds
    ``scitex_store_owner`` for any ordinary agent role; in the throwaway
    cluster it finds the ``scitex_setfalse_owner`` the fixture grants
    ``WITH SET FALSE`` for exactly this purpose.

    Skipped with the reason spelled out where no such role exists — a cluster
    whose every membership carries SET cannot express the bug, and a skip that
    reads as a pass is how a suite reports green while measuring nothing.
    """
    rows = raw_query(
        "SELECT r.rolname FROM pg_roles r WHERE r.rolname <> current_user "
        "AND pg_has_role(current_user, r.oid, 'USAGE') "
        "AND NOT pg_has_role(current_user, r.oid, 'SET') ORDER BY 1"
    )
    if not rows:
        pytest.skip(
            "no role in this cluster that the test session inherits but "
            "cannot SET ROLE to, so the PostgreSQL 16 INHERIT/SET split "
            "cannot be reproduced here"
        )
    return str(rows[0][0])


def test_an_owner_this_session_cannot_set_role_to_is_refused(
    tmp_path: Path, pg_schema: str, unsettable_owner: str
) -> None:
    """The run must stop: it could not hand the tables over afterwards."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    rc = run(db, "--commit", "--table-owner", unsettable_owner)
    # Assert
    assert rc == 1


def test_that_refusal_creates_no_table_at_all(
    tmp_path: Path, pg_schema: str, unsettable_owner: str
) -> None:
    """THE PROPERTY THE BUG BROKE, and the reason this file exists.

    Asking 'USAGE' let the session past the gate, so the DDL ran and the
    refusal landed behind it — leaving ``sac_channel_import`` owned by a leaf
    role on the live primary. Refusing is only half the contract; refusing
    having built nothing is the other half.
    """
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit", "--table-owner", unsettable_owner)
    # Assert
    assert raw_query("SELECT to_regclass('sac_channel_events') IS NULL") == [(True,)]


def test_the_ledger_is_not_created_either(
    tmp_path: Path, pg_schema: str, unsettable_owner: str
) -> None:
    """``sac_channel_import`` is the table that actually got stranded."""
    # Arrange
    db = legacy_db(tmp_path, _seed_rows())
    # Act
    run(db, "--commit", "--table-owner", unsettable_owner)
    # Assert
    assert raw_query("SELECT to_regclass('sac_channel_import') IS NULL") == [(True,)]


def test_the_reachability_check_rejects_an_inherit_only_role(
    pg_schema: str, unsettable_owner: str
) -> None:
    """The predicate itself: inheriting is not enough to be handed a table."""
    # Arrange
    owners = script_module("_pg_table_owner")
    # Act
    with raw_conn() as conn:
        problem = owners.owner_is_reachable(conn, owner=unsettable_owner)
    # Assert
    assert problem is not None


def test_the_refusal_explains_inherit_versus_set(
    pg_schema: str, unsettable_owner: str
) -> None:
    """The message has to teach the distinction, not just report a failure.

    An operator who reads "not a member of X" goes and checks membership,
    finds it, and concludes the tool is broken — which is roughly what a whole
    evening of break-glass looked like.
    """
    # Arrange
    owners = script_module("_pg_table_owner")
    # Act
    with raw_conn() as conn:
        problem = owners.owner_is_reachable(conn, owner=unsettable_owner)
    # Assert
    assert "SET ROLE" in problem


def test_the_refusal_names_a_role_that_could_run_it(
    pg_schema: str, unsettable_owner: str
) -> None:
    """Naming who CAN is what turns a refusal into an instruction."""
    # Arrange
    owners = script_module("_pg_table_owner")
    capable = raw_query(
        "SELECT rolname FROM pg_roles WHERE rolcanlogin "
        "AND pg_has_role(oid, %s::regrole, 'SET') ORDER BY 1",
        (unsettable_owner,),
    )
    # Act
    with raw_conn() as conn:
        problem = owners.owner_is_reachable(conn, owner=unsettable_owner)
    # Assert
    assert all(str(r[0]) in problem for r in capable)


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — asking 'SET' must not refuse a session that CAN reown.
# ---------------------------------------------------------------------------


@pytest.fixture()
def settable_owner(pg_schema: str) -> str:
    """A role this session CAN ``SET ROLE`` to — the other side of the split."""
    rows = raw_query(
        "SELECT r.rolname FROM pg_roles r WHERE r.rolname <> current_user "
        "AND pg_has_role(current_user, r.oid, 'SET') ORDER BY 1 LIMIT 1"
    )
    if not rows:
        pytest.skip("this session can SET ROLE to no other role")
    return str(rows[0][0])


def test_a_settable_owner_is_still_accepted(
    pg_schema: str, settable_owner: str
) -> None:
    """A role this session can SET ROLE to passes, as it always did.

    Without this, tightening 'USAGE' to 'SET' could refuse everything and
    every test above would still be green.
    """
    # Arrange
    owners = script_module("_pg_table_owner")
    # Act
    with raw_conn() as conn:
        problem = owners.owner_is_reachable(conn, owner=settable_owner)
    # Assert
    assert problem is None


def test_the_session_own_role_is_always_reachable(pg_schema: str) -> None:
    """Owning what you already own needs no SET ROLE at all."""
    # Arrange
    owners = script_module("_pg_table_owner")
    me = str(raw_query("SELECT current_user")[0][0])
    # Act
    with raw_conn() as conn:
        problem = owners.owner_is_reachable(conn, owner=me)
    # Assert
    assert problem is None
