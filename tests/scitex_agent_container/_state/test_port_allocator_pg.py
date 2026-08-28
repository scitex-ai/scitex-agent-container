"""The A2A port ledger on a REAL PostgreSQL — never SQLite, never a mock.

Mirrors ``src/scitex_agent_container/_state/port_allocator_pg.py``.

THE TEST THAT CARRIES THE DESIGN
================================
``test_two_agents_cannot_hold_the_same_port``. The obvious port of this
module keys the store on ``name``, and that version passes every other test
in this file while silently losing ``UNIQUE(port)`` — two agents would simply
be two different records. This module exists to stop exactly that collision:
its SQLite predecessor's comments record the v0.21.19 release dying on
``UNIQUE constraint failed: a2a_ports.port``, reproduced at 16 threads as 6
raw driver escapes. So the port is the IDENTITY, and this test is what says
so in a way a future refactor cannot quietly undo.

``test_concurrent_claims_are_all_distinct`` is the same invariant under the
condition that produced the original bug — real threads racing on one range —
because a mutual-exclusion claim that is only tested serially is untested.

Isolation is the shared ``pg_schema`` fixture: a throwaway PostgreSQL schema
selected through ``search_path`` and dropped afterwards, so the live per-host
ledger is never touched. Real ``os.environ``, real store, real database —
PA-306 forbids mocks, and a mocked store here would test nothing at all.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from scitex_agent_container._state.port_allocator_pg import (
    claim_port,
    get_port,
    init_port_schema,
    list_claims,
    release_port,
)


def _claim_in_race_range(agent_name: str) -> int:
    """One racing claimant. A module-level def so the pool can map it."""
    return claim_port(agent_name, range_=(9500, 9599))


# ----------------------------------------------------------------------
# The store exists, and it is PostgreSQL.
# ----------------------------------------------------------------------


def test_init_reports_where_the_state_went(pg_schema: str) -> None:
    # Arrange — the property is that the ledger reports a POSTGRESQL endpoint,
    # not which port that endpoint happens to sit on.
    #
    # This asserted ``"55432" in locator`` — the fleet's per-host PostgreSQL
    # port. That is a fact about the machines the fleet runs on, not about this
    # code, and CI starts its own PostgreSQL on an ephemeral port, so it failed
    # there with:
    #
    #     assert '55432' in 'postgres[host=127.0.0.1 db=postgres port=53177]'
    #
    # Both strings describe a perfectly correct ledger. Pinning the port tests
    # the ENVIRONMENT, passes only where one particular deployment is running,
    # and has to be re-edited every time the fixture moves. The prefix is what
    # actually distinguishes the migrated ledger from the SQLite path it
    # replaced, which is the thing this module exists to hold in place.
    expected_prefix = "postgres["
    # Act
    locator = init_port_schema()
    # Assert
    assert locator.startswith(expected_prefix)


# ----------------------------------------------------------------------
# Claiming.
# ----------------------------------------------------------------------


def test_an_explicit_port_is_claimed(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    # Act
    port = claim_port("zz-a", explicit=9001)
    # Assert
    assert port == 9001


def test_a_claim_is_readable_by_agent_name(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    claim_port("zz-a", explicit=9001)
    # Act
    found = get_port("zz-a")
    # Assert
    assert found == 9001


def test_reclaiming_the_same_port_is_idempotent(pg_schema: str) -> None:
    # Arrange — a second start of one agent must not fail a legitimate
    # re-entry, and must not mutate state.
    init_port_schema()
    claim_port("zz-a", explicit=9001)
    # Act
    again = claim_port("zz-a", explicit=9001)
    # Assert
    assert again == 9001


def test_an_agent_holds_exactly_one_claim_after_reclaim(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    claim_port("zz-a", explicit=9001)
    # Act
    claim_port("zz-a", explicit=9001)
    # Assert
    assert len([c for c in list_claims() if c["name"] == "zz-a"]) == 1


def test_auto_scan_returns_a_port_in_range(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    # Act
    port = claim_port("zz-a", range_=(9200, 9210))
    # Assert
    assert 9200 <= port <= 9210


def test_auto_scan_skips_a_taken_port(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    claim_port("zz-holder", explicit=9200)
    # Act
    port = claim_port("zz-a", range_=(9200, 9210))
    # Assert
    assert port != 9200


def test_an_exhausted_range_raises(pg_schema: str) -> None:
    # Arrange — a one-port range, already held.
    init_port_schema()
    claim_port("zz-holder", explicit=9300)
    # Act
    attempt = partial(claim_port, "zz-a", range_=(9300, 9300))
    # Assert
    with pytest.raises(RuntimeError, match="no free a2a port"):
        attempt()


# ----------------------------------------------------------------------
# THE INVARIANT.
# ----------------------------------------------------------------------


def test_two_agents_cannot_hold_the_same_port(pg_schema: str) -> None:
    """THE DESIGN TEST — see the module docstring.

    A store keyed on ``name`` passes every other test here and fails this
    one, because there the two claims are simply two records.
    """
    # Arrange
    init_port_schema()
    claim_port("zz-first", explicit=9400)
    try:
        claim_port("zz-second", explicit=9400, explicit_is_pin=True)
    except RuntimeError:
        pass
    # Act
    holders = [c["name"] for c in list_claims() if c["port"] == 9400]
    # Assert
    assert holders == ["zz-first"]


# THE XFAIL IS GONE BECAUSE THE UPSTREAM DEFECT IS FIXED.
#
# This carried, from 2026-08-24 until today:
#   xfail(reason="BLOCKED UPSTREAM on scitex_dev.store 0.56.5 — concurrent
#   same-node writes race the oplog (origin, seq) allocation and escape as a
#   raw psycopg UniqueViolation ... Do NOT merge this module while this
#   xfails — the SQLite version it replaces handles this case, and shipping
#   it would reintroduce the bug that killed v0.21.19.")
#
# Its author wrote: "xfail turns green the moment the store is fixed, so this
# file tells us instead of us having to remember." That is exactly what
# happened. scitex-dev 0.56.6 added the bounded (origin, seq) oplog-allocation
# retry; measured today on 0.56.8 against a writable primary, this file gives
#   14 passed, 1 xpassed
# with the xpassed one being this test. The marker is removed rather than left
# in place because an XPASS is not an assertion: strict=False means a
# regression here would silently go back to reading as an expected failure.
def test_concurrent_claims_are_all_distinct(pg_schema: str) -> None:
    """Mutual exclusion under the condition that produced the original bug.

    The SQLite predecessor's TOCTOU reproduced deterministically at 16
    threads; a serial-only test would have passed against it.
    """
    # Arrange
    init_port_schema()
    names = [f"zz-race-{i}" for i in range(12)]
    # Act
    with ThreadPoolExecutor(max_workers=12) as pool:
        claim = partial(_claim_in_race_range)
        ports = list(pool.map(claim, names))
    # Assert
    assert len(set(ports)) == len(ports)


# ----------------------------------------------------------------------
# Pins vs preferences — the distinction a routine restart once died on.
# ----------------------------------------------------------------------


def test_a_pinned_port_held_by_another_agent_raises(pg_schema: str) -> None:
    # Arrange — an operator pin is a contract; a foreign holder is a real
    # misconfiguration and must be visible.
    init_port_schema()
    claim_port("zz-holder", explicit=9600)
    # Act
    attempt = partial(claim_port, "zz-a", explicit=9600, explicit_is_pin=True)
    # Assert
    with pytest.raises(RuntimeError, match="cannot pin"):
        attempt()


def test_a_non_pinned_port_held_by_another_agent_falls_through(
    pg_schema: str,
) -> None:
    # Arrange — a port we merely held before a restart is a preference. If it
    # was taken while we were down, a NEW port is the right answer; failing
    # the launch is not.
    init_port_schema()
    claim_port("zz-holder", explicit=9700)
    # Act
    port = claim_port(
        "zz-a", explicit=9700, explicit_is_pin=False, range_=(9700, 9710)
    )
    # Assert
    assert port != 9700


# ----------------------------------------------------------------------
# Releasing.
# ----------------------------------------------------------------------


def test_release_reports_that_it_released(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    claim_port("zz-a", explicit=9800)
    # Act
    released = release_port("zz-a")
    # Assert
    assert released is True


def test_release_is_idempotent(pg_schema: str) -> None:
    # Arrange
    init_port_schema()
    claim_port("zz-a", explicit=9800)
    release_port("zz-a")
    # Act
    again = release_port("zz-a")
    # Assert
    assert again is False


def test_a_released_port_can_be_claimed_by_another_agent(pg_schema: str) -> None:
    # Arrange — the whole point of releasing.
    init_port_schema()
    claim_port("zz-a", explicit=9800)
    release_port("zz-a")
    # Act
    port = claim_port("zz-b", explicit=9800)
    # Assert
    assert port == 9800
