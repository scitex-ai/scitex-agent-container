"""The store, against a REAL postgres — no mock stands in for the database.

The behaviours under test here are database behaviours: an ``ON CONFLICT``
target that must exist, an owner-partitioned ``UPDATE`` predicate, and two
aggregate queries that answer CR-001. A fake connection would assert that
the strings are the strings, which is exactly the kind of green that this
whole domain exists to stop trusting.

If no postgres is reachable the suite SKIPS with the reason spelled out.
It never silently passes: a skipped database test and a green database
test must not look alike.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._credstate import _store
from scitex_agent_container._credstate._material import CredentialMaterialError
from scitex_agent_container._credstate._model import (
    CredentialDescriptor,
    CredentialObservation,
    CredentialPlacement,
)

TEST_DSN = os.environ.get(
    "SAC_CREDSTATE_TEST_DSN",
    "postgresql://scitex_cards@127.0.0.1:55432/scitex_state_test_credstate",
)

PRIMARY = "scitex-nas-03"
REPLICA = "scitex-compute-04"
OTHER = "ywata-note-win"
KEY = "anthropic-oauth:test"
FAKE_ANTHROPIC = "sk-ant-" + "A" * 40


@pytest.fixture
def conn():
    """A real connection with the schema asserted, truncated per test."""
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip(
            "psycopg (v3) is not installed, so the credential store cannot be "
            "exercised against a real database. Install "
            "scitex-agent-container[state]."
        )
    try:
        connection = _store.open_store(TEST_DSN)
    except Exception as exc:  # noqa: BLE001 - reason must reach the operator
        pytest.skip(
            f"no postgres reachable at {TEST_DSN} ({type(exc).__name__}), so "
            f"the credential-store behaviours were NOT verified. This is a "
            f"skip, not a pass."
        )
    with connection.cursor() as cur:
        for table in (
            "credential_observation",
            "credential_placement",
            "credential_descriptor",
        ):
            cur.execute(f"TRUNCATE {table}")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _descriptor(**kwargs):
    base = dict(
        origin_node=PRIMARY,
        cred_key=KEY,
        account="test",
        tier="primary_secret",
        primary_node=PRIMARY,
    )
    base.update(kwargs)
    return CredentialDescriptor(**base)


def test_the_schema_is_created_and_a_descriptor_round_trips(conn):
    # Arrange
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    rows = _store.descriptors(conn)
    # Assert
    assert [r["cred_key"] for r in rows] == [KEY]


def test_a_repeated_identical_declaration_does_not_duplicate(conn):
    # Arrange — DO NOTHING on conflict, never a blind UPDATE.
    _store.record_descriptor(conn, _descriptor())
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    rows = _store.descriptors(conn)
    # Assert
    assert len(rows) == 1


def test_a_placement_is_readable_by_node(conn):
    # Arrange
    _store.record_placement(
        conn,
        CredentialPlacement(
            origin_node=REPLICA,
            cred_key=KEY,
            node=REPLICA,
            role="replica",
            locator="file:/home/agent/.claude/.credentials.json",
        ),
    )
    conn.commit()
    # Act
    rows = _store.placements_for(conn, REPLICA)
    # Assert
    assert rows[0]["locator"] == "file:/home/agent/.claude/.credentials.json"


def test_a_placement_for_another_node_is_not_returned(conn):
    # Arrange
    _store.record_placement(
        conn,
        CredentialPlacement(
            origin_node=REPLICA, cred_key=KEY, node=REPLICA, locator="file:/x"
        ),
    )
    conn.commit()
    # Act
    rows = _store.placements_for(conn, PRIMARY)
    # Assert
    assert rows == []


def test_an_observation_is_appended_and_readable(conn):
    # Arrange
    _store.record_observation(
        conn,
        CredentialObservation(
            origin_node=REPLICA,
            cred_key=KEY,
            node=REPLICA,
            present=True,
            holds_refresh_material=True,
            verdict="EXTRA_REFRESHER",
        ),
    )
    conn.commit()
    # Act
    holders = _store.refresh_holders(conn, cred_key=KEY)
    # Assert
    assert holders == [REPLICA]


def test_a_node_not_holding_refresh_material_is_not_a_holder(conn):
    # Arrange
    _store.record_observation(
        conn,
        CredentialObservation(
            origin_node=REPLICA,
            cred_key=KEY,
            node=REPLICA,
            present=True,
            holds_refresh_material=False,
            verdict="OK",
        ),
    )
    conn.commit()
    # Act
    holders = _store.refresh_holders(conn, cred_key=KEY)
    # Assert
    assert holders == []


def test_a_single_declared_primary_raises_no_cr001_violation(conn):
    # Arrange
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    violations = _store.cr001_violations(conn)
    # Assert
    assert violations == []


def test_two_declared_primaries_are_reported_as_a_cr001_violation(conn):
    # Arrange — the invariant made checkable by a query.
    _store.record_descriptor(conn, _descriptor())
    _store.record_descriptor(conn, _descriptor(origin_node=OTHER, primary_node=OTHER))
    conn.commit()
    # Act
    violations = _store.cr001_violations(conn)
    # Assert
    assert violations[0]["primary_count"] == 2


def test_the_cr001_violation_names_both_claimants(conn):
    # Arrange
    _store.record_descriptor(conn, _descriptor())
    _store.record_descriptor(conn, _descriptor(origin_node=OTHER, primary_node=OTHER))
    conn.commit()
    # Act
    violations = _store.cr001_violations(conn)
    # Assert
    assert PRIMARY in violations[0]["nodes"] and OTHER in violations[0]["nodes"]


def test_a_tombstoned_declaration_stops_counting_toward_cr001(conn):
    # Arrange — retiring a declaration must clear the alarm.
    _store.record_descriptor(conn, _descriptor())
    _store.record_descriptor(conn, _descriptor(origin_node=OTHER, primary_node=OTHER))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential_descriptor SET deleted_at = now() "
            "WHERE origin_node = %(o)s",
            {"o": OTHER},
        )
    conn.commit()
    # Act
    violations = _store.cr001_violations(conn)
    # Assert
    assert violations == []


def test_two_origins_declaring_one_credential_are_reported_as_divergent(conn):
    # Arrange — reported for adjudication, never auto-merged.
    _store.record_descriptor(conn, _descriptor())
    _store.record_descriptor(conn, _descriptor(origin_node=OTHER))
    conn.commit()
    # Act
    divergent = _store.divergent_declarations(conn)
    # Assert
    assert divergent[0]["cred_key"] == KEY


def test_a_node_may_update_a_row_it_authored(conn):
    # Arrange
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    changed = _store.update_descriptor(
        conn, cred_key=KEY, origin_node=PRIMARY, node_id=PRIMARY, note="own edit"
    )
    # Assert
    assert changed == 1


def test_an_owner_update_bumps_the_revision(conn):
    # Arrange
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    _store.update_descriptor(
        conn, cred_key=KEY, origin_node=PRIMARY, node_id=PRIMARY, note="own edit"
    )
    conn.commit()
    # Act
    rows = _store.descriptors(conn)
    # Assert
    assert rows[0]["revision"] == 2


def test_a_node_may_not_update_a_row_another_node_authored(conn):
    # Arrange — ADR-0022 §5.2 rule 3, single-writer partitioning.
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    # Assert
    with pytest.raises(PermissionError):
        _store.update_descriptor(
            conn, cred_key=KEY, origin_node=PRIMARY, node_id=REPLICA, note="hijack"
        )


def test_an_update_carrying_material_is_refused(conn):
    # Arrange — the guard sits on the UPDATE path too, not only INSERT.
    _store.record_descriptor(conn, _descriptor())
    conn.commit()
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        _store.update_descriptor(
            conn,
            cred_key=KEY,
            origin_node=PRIMARY,
            node_id=PRIMARY,
            note=FAKE_ANTHROPIC,
        )


def test_a_descriptor_carrying_material_never_reaches_the_database(conn):
    # Arrange
    bad = _descriptor(note=FAKE_ANTHROPIC)
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        _store.record_descriptor(conn, bad)


def test_the_latest_observation_wins_per_node(conn):
    # Arrange — the holder set must reflect the newest measurement.
    from datetime import datetime, timedelta, timezone

    early = datetime.now(timezone.utc) - timedelta(hours=1)
    _store.record_observation(
        conn,
        CredentialObservation(
            origin_node=REPLICA,
            cred_key=KEY,
            node=REPLICA,
            observed_at=early,
            present=True,
            holds_refresh_material=True,
            verdict="EXTRA_REFRESHER",
        ),
    )
    _store.record_observation(
        conn,
        CredentialObservation(
            origin_node=REPLICA,
            cred_key=KEY,
            node=REPLICA,
            present=True,
            holds_refresh_material=False,
            verdict="OK",
        ),
    )
    conn.commit()
    # Act
    holders = _store.refresh_holders(conn, cred_key=KEY)
    # Assert
    assert holders == []
