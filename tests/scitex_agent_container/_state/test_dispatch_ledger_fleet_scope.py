"""Two agents, ONE store: a dispatch read must answer for the reader only.

These four tests were written on branch ``claude/wf_53dfcc82-70b-4`` as a
SPECIFICATION, before the ledger moved. Three of them were RED, all stopping
in the ARRANGE with ``TypeError: record_dispatch() got an unexpected keyword
argument 'agent'``; the fourth was a positive control that passed. The port
that moved ``dispatches`` onto ``scitex_dev.store`` added the field they
demanded, so all four are green here — with the same assertions, which is the
only way that means anything.

THE DEFECT THEY PIN
===================
The two backends have opposite shapes and the migration joins them:

  * ``state.db`` was PER-AGENT. Every agent got its own SQLite file, so
    ``list_dispatches()`` with no filters meant "my dispatches" — the SHARD
    did the scoping, and no code had to.
  * ``SCITEX_STORE_DSN`` is FLEET-WIDE. ``runtimes/_fleet_env.py`` injects one
    value into every container and ``scitex_dev.store`` resolves it first. One
    endpoint, one ``dispatches_rows`` table, every agent writing into it.

Ported as-is, 130+ per-agent shards become ONE shared table with no ownership
column. ``list_dispatches()`` then returns the WHOLE FLEET's outbound traffic
to whoever asks, and ``list_unreacted_dispatches()`` reports every other
agent's comm-misses as this agent's own. NOTHING RAISES. No column is missing,
no query errors, no store is unreachable — the answers just quietly become
wrong, and wrong in the direction of "there is much more traffic than I sent",
which reads as data rather than as a bug.

WHY THE EXISTING COLUMNS CANNOT DO IT
=====================================
``from_agent`` looks like the owner and is not:

  * it is the SENDER of one message. The ledger row is the identity of one
    outbound SEND ACTION, and the process that owns the ledger is not always
    the one named in the row.
  * it is EXPLICITLY NULLABLE, and that is a supported case with a test of its
    own beside this file — ``test_record_dispatch_allows_null_agents``, whose
    docstring states the reason: "a script driving post_turn outside an agent
    has no SAC_NAME". ``_network/_peer_dispatch.self_agent_name`` returns
    ``None`` there and the row records ``from_agent=None``.

A NULL owner is unfilterable by construction. In a per-agent shard that row is
still findable, because the FILE names its owner; in one shared table it
belongs to nobody and is returned to everybody. So the fix could not be
"filter on ``from_agent``" — the field is the wrong thing and is allowed to be
absent.

THE SHAPE THAT WAS COPIED
=========================
``_state/inbound_ledger.py`` — the receiver-side mirror of this same ledger —
hit this exact problem when it moved (the FOURTH table to move) and solved it
by carrying the owner in the store IDENTITY: ``agent`` is the first of its
``IDENTITY_FIELDS``, it is ``required=True`` / ``MergeRule.IMMUTABLE``,
``record_inbound`` refuses an empty one, and its production reader
``claim_oldest_pending(*, agent)`` cannot be called without it. That is the
shape these tests asked ``dispatch_ledger`` for, and what it now has:
``IDENTITY_FIELDS = ("agent", "dispatch_id")``. The kwarg is spelled ``agent``
for that reason and no other.

WHAT ONE SHARED STORE MEANS NOW
===============================
The original fixture pointed BOTH agents at a SINGLE ``state.db``, to
reproduce in SQLite the condition the migration was about to create. It is no
longer a reproduction: ``pg_schema`` gives the test one real PostgreSQL store,
which is literally the post-migration shape, and the module resolves it
itself. The test bodies are unchanged, exactly as that branch predicted —
"after the move the same calls address the shared PostgreSQL store and must
still answer with one agent's rows".

WHAT THESE TESTS DO NOT PROVE
=============================
They do not prove a leak ever happened in production. Measured across ``src/``
before they were written: ``list_dispatches`` and ``list_unreacted_dispatches``
have ZERO production callers — they are the documented recall / comm-miss READ
surface (``_network/peer.py`` names ``list_dispatches(to_agent=...)`` in prose)
exercised by tests. The leak was LATENT: it would have arrived with the port,
not before it. That is the argument for pinning it in the same PR rather than
after, and it is also why this was a blocker and not an incident.

They also say nothing about the WRITE path. ``record_dispatch`` /
``update_dispatch_status`` key on a uuid4 ``dispatch_id``, which stays unique
across a shared table, so writes do not collide. Only the reads leaked.

Conventions, matching the two ``test_dispatch_ledger*`` modules beside this
one: AAA markers (STX-TQ002), one assertion per test (STX-TQ007), no mocks and
no monkeypatch (PA-306 / STX-NM002) — a real PostgreSQL schema per test.
"""

from __future__ import annotations

import time

#: One agent's identity, and another's. Two names, ONE store — the whole point.
AGENT_A = "agent-alpha"
AGENT_B = "agent-beta"

#: Comfortably older than the SLO used below, so a recorded row is already
#: "stale" the moment it lands and ``list_unreacted_dispatches`` considers it.
_SLO_S = 30.0
_WELL_PAST_SLO_S = 600.0


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — the fixture works, and the leak is visible without it.
# ---------------------------------------------------------------------------


def test_one_shared_store_returns_both_agents_dispatches(pg_schema: str):
    """The control that makes the other three trustworthy.

    Two things a reader cannot otherwise tell apart:

      * the three tests below pass because the OWNING-AGENT FIELD EXISTS, not
        because the fixture silently wrote nothing. A control that failed
        would mean the fixture, not the ledger.
      * an UNFILTERED read over a shared ledger legally sees the WHOLE ledger,
        which is what ``list_inbound(agent=None)`` does in the already-migrated
        mirror module. The fix was never to make the unfiltered read lie; it
        was to give callers a way to ask for their own.

    Written with the pre-migration spelling on purpose — no ``agent`` kwarg
    anywhere — so it also pins that omitting the owner stays legal.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent=AGENT_A, to_agent="peer-x", text="mine")
    record_dispatch(from_agent=AGENT_B, to_agent="peer-y", text="theirs")
    # Act
    rows = list_dispatches()
    # Assert
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# list_dispatches — the recall surface.
# ---------------------------------------------------------------------------


def test_list_dispatches_returns_only_the_owning_agents_rows(pg_schema: str):
    """Two agents, one store: alpha's recall must not contain beta's send.

    The assertion is an EQUALITY on the id list, not a membership test, so it
    fails both ways it can be wrong — a missing own row and a leaked foreign
    one. A filter that returned nothing would satisfy "no leak" and is not
    what is being asked for.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    mine = record_dispatch(
        agent=AGENT_A, from_agent=AGENT_A, to_agent="peer-x", text="mine"
    )
    record_dispatch(agent=AGENT_B, from_agent=AGENT_B, to_agent="peer-y", text="theirs")
    # Act
    rows = list_dispatches(agent=AGENT_A)
    # Assert
    assert [row["dispatch_id"] for row in rows] == [mine]


# ---------------------------------------------------------------------------
# list_unreacted_dispatches — the comm-miss surface.
# ---------------------------------------------------------------------------


def test_list_unreacted_dispatches_returns_only_the_owning_agents_rows(
    pg_schema: str,
):
    """A comm-miss report must not accuse this agent of the fleet's misses.

    This surface leaks worse than plain recall. Its documented use is "absence
    of a reaction past the SLO = the recipient never injected the message", so
    a fleet-wide answer does not merely add noise — it manufactures alerts
    about peers this agent never dispatched to, and the natural response to
    one of those is to re-send.

    Both rows are backdated well past the SLO so staleness cannot be what
    separates them; only ownership can.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    stale_ts = time.time() - _WELL_PAST_SLO_S
    mine = record_dispatch(
        agent=AGENT_A, from_agent=AGENT_A, to_agent="peer-x", text="mine", ts=stale_ts
    )
    record_dispatch(
        agent=AGENT_B, from_agent=AGENT_B, to_agent="peer-y", text="theirs", ts=stale_ts
    )
    # Act
    rows = list_unreacted_dispatches(older_than_s=_SLO_S, agent=AGENT_A)
    # Assert
    assert [row["dispatch_id"] for row in rows] == [mine]


# ---------------------------------------------------------------------------
# The anonymous row — why from_agent cannot be the discriminator.
# ---------------------------------------------------------------------------


def test_an_anonymous_dispatch_does_not_leak_to_another_agent(pg_schema: str):
    """``from_agent=None`` is supported, so ownership must live elsewhere.

    Alpha records the supported anonymous row — the "script driving post_turn
    outside an agent container" case, which
    ``test_record_dispatch_allows_null_agents`` beside this file pins as legal.
    Beta then asks for its own dispatches.

    A ``from_agent``-based filter cannot exclude a NULL sender from beta's
    answer without also hiding it from alpha's, which is why this test is here
    and not folded into the first: it fails the obvious cheap fix, on purpose.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(agent=AGENT_A, from_agent=None, to_agent=None, text="anonymous")
    theirs = record_dispatch(
        agent=AGENT_B, from_agent=AGENT_B, to_agent="peer-y", text="theirs"
    )
    # Act
    rows = list_dispatches(agent=AGENT_B)
    # Assert
    assert [row["dispatch_id"] for row in rows] == [theirs]
