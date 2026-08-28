#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""comms_grants on PostgreSQL — and a revoke that actually denies.

This is an ACL table, so the tests that matter are the FLIPS: not "a grant
can be written" but "after a revoke, the predicate the gate calls says NO".
A migration that stored grants perfectly and forgot to deny would read as
green on every round-trip assertion and be a security regression.

THREE PROPERTIES THE SQLITE VERSION HAD THAT ARE EASY TO LOSE HERE, each
with its own test below:

  * REVOKE DENIES. `revoke_send` no longer DELETEs — the store's only
    removal is `hide`. If `has_grant` read hidden rows it would keep
    authorising a withdrawn grant, which is the worst failure this file can
    catch.
  * REVOKE NO LONGER FORGETS. Under DELETE, "never granted" and "granted
    then revoked" were indistinguishable. The hidden row keeps the history,
    so the audit question is answerable.
  * THE LISTING ORDER IS CAUSAL, NOT WALL-CLOCK. The SQLite docstring
    recorded why it ordered by rowid: `created_at` ties on bulk-imported
    peer rows and skews across hosts, and a foreign row then sorted into a
    plausible position instead of standing out — which is what let a leaked
    `-> lead` grant hide in the listing. `test_listing_order_is_insertion_
    order_not_created_at` writes a row whose created_at is OLDER than one
    already stored and asserts it still lists LAST. Ordering by created_at
    fails that test; ordering by the HLC passes it.

Needs a real PostgreSQL: `pg_schema` is the shared opt-in fixture, which
skips where no cluster exists and FAILS where a configured one is broken.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real public
surface, and isolation comes from the fixture pointing SCITEX_STORE_DSN at a
throwaway schema.
"""

from __future__ import annotations

import time

from scitex_agent_container._state.state_db_grants import (
    grant_send,
    has_grant,
    list_comms_grants,
    revoke_send,
)


# ---------------------------------------------------------------------------
# the flip
# ---------------------------------------------------------------------------


def test_a_grant_authorises(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta", note="t")
    # Act
    allowed = has_grant(sender="alpha", target="beta")
    # Assert
    assert allowed is True


def test_a_revoked_grant_no_longer_authorises(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta", note="t")
    # Act
    revoke_send(sender="alpha", target="beta")
    # Assert — the whole point of the module.
    assert has_grant(sender="alpha", target="beta") is False


def test_revoke_reports_true_when_a_live_grant_was_withdrawn(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta")
    # Act
    removed = revoke_send(sender="alpha", target="beta")
    # Assert
    assert removed is True


def test_revoke_reports_false_for_a_pair_never_granted(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta")
    # Act
    removed = revoke_send(sender="ghost", target="nobody")
    # Assert
    assert removed is False


def test_revoke_reports_false_the_second_time(pg_schema: str) -> None:
    # Arrange — the hidden row still occupies the identity, so a naive
    # "does the record exist" check would answer True and lie.
    grant_send(sender="alpha", target="beta")
    revoke_send(sender="alpha", target="beta")
    # Act
    again = revoke_send(sender="alpha", target="beta")
    # Assert
    assert again is False


# ---------------------------------------------------------------------------
# revoke stops authorising without forgetting
# ---------------------------------------------------------------------------


def test_a_revoked_grant_is_still_on_record(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_grants import _open

    grant_send(sender="alpha", target="beta", note="why-it-was-granted")
    revoke_send(sender="alpha", target="beta")
    # Act
    store = _open()
    try:
        row = store.get(
            {"sender_name": "alpha", "target_name": "beta"}, include_hidden=True
        )
    finally:
        store.close()
    # Assert — DELETE could not answer this.
    assert row is not None and row.values["note"] == "why-it-was-granted"


def test_a_revoked_grant_is_absent_from_the_listing(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta")
    grant_send(sender="gamma", target="delta")
    revoke_send(sender="alpha", target="beta")
    # Act
    pairs = {(r["sender"], r["target"]) for r in list_comms_grants()}
    # Assert
    assert pairs == {("gamma", "delta")}


# ---------------------------------------------------------------------------
# idempotence, in both directions
# ---------------------------------------------------------------------------


def test_regranting_a_live_pair_does_not_move_the_timestamp(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta")
    first = list_comms_grants()[0]["created_at"]
    time.sleep(0.01)
    # Act
    grant_send(sender="alpha", target="beta", note="second attempt")
    # Assert
    assert list_comms_grants()[0]["created_at"] == first


def test_regranting_a_revoked_pair_restores_authorisation(pg_schema: str) -> None:
    # Arrange — impossible to express under DELETE; the row is hidden, and
    # an insert would collide with the identity it still occupies.
    grant_send(sender="alpha", target="beta")
    revoke_send(sender="alpha", target="beta")
    # Act
    grant_send(sender="alpha", target="beta")
    # Assert
    assert has_grant(sender="alpha", target="beta") is True


def test_an_empty_sender_is_refused(pg_schema: str) -> None:
    # Arrange
    refused = None
    # Act
    try:
        grant_send(sender="", target="beta")
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None


def test_has_grant_is_false_for_an_empty_target(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="alpha", target="beta")
    # Act
    allowed = has_grant(sender="alpha", target="")
    # Assert
    assert allowed is False


# ---------------------------------------------------------------------------
# the ordering property that once hid a leaked grant
# ---------------------------------------------------------------------------


def test_listing_order_is_insertion_order_not_created_at(pg_schema: str) -> None:
    # Arrange — write a row carrying an OLDER created_at than one already
    # stored, the way import_state carries a peer's timestamp verbatim.
    from scitex_dev.store import NEW_RECORD

    from scitex_agent_container._state.state_db_grants import _open

    grant_send(sender="local", target="first")
    store = _open()
    try:
        store.put(
            {
                "sender_name": "imported",
                "target_name": "second",
                "created_at": 1.0,  # far older than the row above
                "note": "peer row with a skewed clock",
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    # Act
    order = [(r["sender"], r["target"]) for r in list_comms_grants()]
    # Assert — created_at ordering would put the imported row FIRST, which
    # is precisely how a foreign grant used to hide in this listing.
    assert order == [("local", "first"), ("imported", "second")]
