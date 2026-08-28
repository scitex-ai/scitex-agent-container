#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The pinned-port restart round trip, and the store behaviour that endangers it.

WHAT THIS FILE PINS
===================
One invariant, stated once and tested twice from two vantages:

    claim(pin) -> release -> re-claim(SAME pin)  MUST SUCCEED.

An operator who writes ``spec.a2a.port: 19100`` has stated a contract. Every
ordinary restart runs ``agent_stop`` (which calls ``release_a2a_port`` ->
``release_port``) and then ``agent_start`` (which calls ``resolve_a2a_port``
-> ``claim_port(explicit=19100, explicit_is_pin=True)``). If that second
claim raises, the agent NEVER COMES BACK. Every pinned agent on the fleet is
one restart away from staying down, which is why this round trip earns a
file of its own rather than a line in the allocator's own test module.

HONEST STATUS OF EACH TEST HERE — READ THIS BEFORE TRUSTING A GREEN RUN
======================================================================
THE TABLE HAS NOW MOVED (2026-08-28). When this file was written
``_state/port_allocator.py`` still owned a SQLite table and ``release_port``
still issued ``DELETE FROM a2a_ports``, which freed the row outright — so the
round trip below passed for a reason that disappeared the moment the table
moved. It moved; the round trip still passes, and it now passes for the
reason this file demanded: ``port_allocator_store.try_claim`` UNHIDES the
tombstone instead of reading it as held.

What that changes here is ONLY the plumbing. The round-trip test used to
thread a ``tmp_path`` SQLite file through ``db_path``; ``db_path`` is gone
from the allocator's signatures because it named a file that no longer
exists, so the test now takes ``pg_schema`` like the three below it. The
ARRANGE, the ACT and the single assertion are untouched — the invariant is
the same invariant, measured through the same public surface.

The hazard this file exists for was never in ``_state/port_allocator.py``. It
is a hazard in the MAPPING, and it lives in ``scitex_dev.store``. Three tests
below record it directly, so that it stays measured rather than remembered.

THE HAZARD, MEASURED
====================
``scitex_dev/store/_store.py`` reads with ``include_hidden=True`` on the
write path::

    def put(self, values, *, expected_revision, ...):
        record = record_key(self.schema, values)
        current = self._read(record, include_hidden=True)   # <- here
        check_revision(record, current, self._revision(record), expected_revision)

The store's ONLY removal is ``hide`` — there is no delete — so a migrated
``release_port`` must hide the claim. A hidden claim is a TOMBSTONE that
still occupies the store identity, and the two doors then disagree:

  * ``get(key)`` answers ``None``           -> the record reads as ABSENT
  * ``put(key, NEW_RECORD)`` raises
    ``RevisionMismatchError``                -> the identity is TAKEN

That disagreement is what kills the restart, and it kills it TWICE over —
both halves are recorded below:

  1. The idempotent fast path in ``claim_port`` asks ``get``, is told the
     agent holds nothing, and falls through to the create.
  2. The create asserts ``NEW_RECORD`` and is refused, so no row is written.
  3. The holder read-back then answers wrongly whichever view it uses:
       * a LIVE-only scan (``rows()``) reports NOBODY holds the port, the
         pin branch takes ``holder is None``, and the operator is told the
         port is ``already claimed by 'another agent'``;
       * a hidden-INCLUSIVE scan (``rows(include_hidden=True)``) reports
         ``alpha`` holds it, and a guard shaped ``holder is not None and
         not hidden`` rejects the tombstone as not-live — so the operator
         is told the port is ``already claimed by 'alpha'``, BY ITSELF.
     Both spellings end in ``RuntimeError`` and a pinned agent that never
     restarts.

THE CORRECT MAPPING, so nobody has to rediscover it: ``unhide``.
``_state/state_db_grants.py`` already does exactly this for the same reason
and says so at its ``grant_send`` — read hidden-inclusive, ``unhide`` if a
tombstone occupies the identity, ``put(NEW_RECORD)`` only when genuinely
absent. ``state_db_pending_approval.py`` and ``state_db_blocks.py`` use the
three-valued ``is_hidden`` for the same distinction.

WHY THERE WAS NO RED TEST HERE, STATED PLAINLY
==============================================
Only two things would have made one, and both were rejected:

  * MIGRATING the table, which the work that wrote this file was explicitly
    forbidden to do. A half-migrated ``a2a_ports`` is a split brain that
    raises nothing: some readers see a row and others do not. (The migration
    landed later, as one PR moving every reader together.)
  * ASSERTING that ``put(..., NEW_RECORD)`` should succeed after a ``hide``.
    That is not a defect in ``scitex_dev.store``; it is its documented
    design ("'Hidden' and 'absent' remain distinguishable — a caller that
    cannot tell them apart will eventually treat one as the other", and the
    refusal's own text, "if a create was intended, the id is taken"). A test
    asserting the opposite would be red forever, could not be made green by
    any correct migration, and would be a false defect filed against another
    package.

So the three store-level tests below are CHARACTERISATION tests of a
third-party primitive: each asserts what the store measurably does today.
Their value is that they turn RED the day that behaviour changes — at which
point the hazard is gone and this file should be revisited.

VANTAGE: A REAL POSTGRESQL, NEVER THE STORE'S SQLITE DIALECT
============================================================
``pg_schema`` (the shared opt-in fixture in ``tests/_store_isolation.py``)
points ``SCITEX_STORE_DSN`` at a throwaway schema. The store's SQLite
dialect would have run on every machine and was deliberately NOT used:
``test_state_db_verdict_dedup.py`` records why — "a suite that exercised the
store's SQLite dialect instead would be testing a code path production can
never take", and scitex-dev 0.49.0 shipped a PostgreSQL backend that could
be written to and never read from precisely because nobody read back through
the dialect production uses.

The cost is stated rather than hidden: ``pg_schema`` SKIPS wherever there is
no writable PostgreSQL, and per the operator's 2026-08-26 ruling every fleet
host's loopback is now a READ-ONLY REPLICA of the one primary. So EVERY test
in this file — the round trip included, now that the allocator has no SQLite
path left to fall back on — skips on a host with no writable database, and
only executes where one is provisioned. A skip is not a pass; that is the
whole reason this paragraph exists instead of a green tick. Point
``SAC_TEST_PG_DSN`` at a throwaway cluster to make them run, which also
turns an unusable target from a skip into a hard failure.

NO MOCKS, NO MONKEYPATCH (PA-306 / STX-NM002). The allocator is exercised
through its real public surface; the store is the real store, isolated by the
fixture pointing the REAL resolver at a throwaway schema.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from scitex_agent_container._state import port_allocator as pa

#: The port the tests pin. Well outside ``DEFAULT_RANGE`` (19000-19999) so a
#: stray real claim can never collide with it.
PINNED_PORT = 21500

#: The agent that owns the pin in every test here.
AGENT = "alpha"

#: Write attribution for the store-level probes, matching what every migrated
#: module in ``_state`` uses.
_ACTOR = "scitex-agent-container"


def _probe_schema() -> Any:
    """The shape a migrated ``a2a_ports`` record would have.

    Deliberately NOT production code and deliberately NOT named
    ``a2a_ports``: this file must pin the hazard WITHOUT migrating the
    table, and a store named for the real table would be the first half of
    exactly the split brain the work was told not to create.

    ``claimed_at`` is epoch ``REAL``, not the ISO text the SQLite column
    holds — the migrated timestamp columns across ``_state`` are all REAL.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def fact(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=True,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=False,
        )

    return Schema(
        name="a2a_ports_reclaim_probe",
        fields={
            # The agent name is the identity, exactly as the SQLite
            # ``name TEXT PRIMARY KEY`` treats it.
            "name": ident(FieldKind.TEXT),
            "port": fact(FieldKind.INTEGER),
            "claimed_at": fact(FieldKind.REAL),
        },
    )


@pytest.fixture
def ports_store(pg_schema: str) -> Iterator[Any]:
    """An open store in the throwaway schema, closed on teardown.

    MULTI_WRITER for the same reason ``state_db_grants`` gives: a claim is
    written by the starting host and released by whoever stops the agent, so
    SINGLE_WRITER would make an ordinary cross-host stop an illegal write.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = _probe_schema()
    store = Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )
    try:
        yield store
    finally:
        store.close()


def _claim_then_release(store: Any) -> None:
    """Put a live claim for ``AGENT``, then hide it — the release a port must use.

    Raises rather than asserts: this is ARRANGE for the tests below, and a
    precondition that fails must not be counted as one of the test's facts.
    """
    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    key = {"name": AGENT}
    store.put(
        {"name": AGENT, "port": PINNED_PORT, "claimed_at": 1.0},
        expected_revision=NEW_RECORD,
    )
    store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
    if store.get(key) is not None:
        raise RuntimeError(
            "arrange failed: after hide(), get() must read the tombstone as "
            "absent. If this raises, the store's read door changed and every "
            "conclusion in this module's docstring needs re-measuring."
        )


# ---------------------------------------------------------------------------
# the invariant, through the allocator's real public surface
# ---------------------------------------------------------------------------


def test_a_pinned_port_is_reclaimed_by_the_same_agent_after_release(
    pg_schema: str,
) -> None:
    """The restart round trip, through the allocator's real public surface.

    Written against the PUBLIC surface and not against the backend precisely
    so it would keep testing the same thing across the move — which is what
    happened. Before the migration it passed because SQLite's ``DELETE``
    freed the row; it now passes because ``port_allocator_store.try_claim``
    UNHIDES the tombstone that ``hide`` leaves behind. Map ``release_port``
    onto ``hide`` and ``claim_port``'s insert onto a bare
    ``put(..., NEW_RECORD)`` and this goes red, with the operator told the
    port is claimed by the very agent asking for it.
    """
    # Arrange — an operator pin, honoured, then released as ``agent_stop`` does.
    first = pa.claim_port(
        AGENT,
        range_=(PINNED_PORT, PINNED_PORT + 9),
        explicit=PINNED_PORT,
        explicit_is_pin=True,
    )
    if first != PINNED_PORT:
        raise RuntimeError(f"arrange failed: the pin was not honoured, got {first}")
    if not pa.release_port(AGENT):
        raise RuntimeError("arrange failed: release_port dropped no claim")
    # Act — the SAME agent re-claims the SAME pinned port, as every restart does.
    reclaimed = pa.claim_port(
        AGENT,
        range_=(PINNED_PORT, PINNED_PORT + 9),
        explicit=PINNED_PORT,
        explicit_is_pin=True,
    )
    # Assert
    assert reclaimed == PINNED_PORT


# ---------------------------------------------------------------------------
# the hazard, measured directly against the store the migration must use
# ---------------------------------------------------------------------------


def test_a_released_claim_still_occupies_the_store_identity(
    ports_store: Any,
) -> None:
    """The write door refuses the re-claim the read door said was free.

    The first half of the defect. ``get`` has already answered ``None`` in
    the arrange step, so a migration that checks "does this agent hold a
    port?" is told no and proceeds to create — and the create is refused,
    because ``put`` reads ``include_hidden=True`` and the tombstone still
    holds the identity. Nothing is written and no port is claimed.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    # Arrange
    _claim_then_release(ports_store)

    # Act — the create a migrated ``claim_port`` makes on the restart.
    def re_claim() -> None:
        ports_store.put(
            {"name": AGENT, "port": PINNED_PORT, "claimed_at": 2.0},
            expected_revision=NEW_RECORD,
        )

    # Assert — measured behaviour today. Red here means the store changed and
    # the hazard is gone; see the module docstring.
    with pytest.raises(RevisionMismatchError):
        re_claim()


def test_a_released_claim_is_absent_from_a_live_holder_scan(
    ports_store: Any,
) -> None:
    """A live-only holder scan reports NOBODY holds the released port.

    The second half, first spelling. ``claim_port`` reads back WHO holds
    ``explicit`` after its insert. Ported onto ``rows()`` — which excludes
    hidden rows by default — the answer is empty, the pin branch takes
    ``holder is None``, and the operator is told the port is ``already
    claimed by 'another agent'`` when in fact nobody holds it at all.
    """
    # Arrange
    _claim_then_release(ports_store)
    # Act
    live_holders = [row.values["name"] for row in ports_store.rows()]
    # Assert
    assert live_holders == []


def test_a_released_claim_is_present_in_a_hidden_inclusive_holder_scan(
    ports_store: Any,
) -> None:
    """A hidden-inclusive holder scan reports the RELEASING agent still holds it.

    The second half, second spelling — and the one that produces the exact
    ``already claimed by <self>`` message. ``rows(include_hidden=True)``
    returns the tombstone, so a guard shaped ``holder is not None and not
    hidden`` rejects it as not-live and falls into the pin branch with
    ``holder["name"] == agent_name``. The operator is then told the port is
    claimed by the very agent asking for it.
    """
    # Arrange
    _claim_then_release(ports_store)
    # Act
    all_holders = [
        row.values["name"] for row in ports_store.rows(include_hidden=True)
    ]
    # Assert
    assert all_holders == [AGENT]
