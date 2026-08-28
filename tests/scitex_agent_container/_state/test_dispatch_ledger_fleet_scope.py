"""The dispatch ledger cannot say WHOSE a dispatch is — and that blocks its move.

THREE OF THESE FOUR TESTS FAIL TODAY, ON PURPOSE. They are a specification of
the one property ``_state/dispatch_ledger.py`` must gain BEFORE its table moves
to PostgreSQL, not a description of what it does. The fourth is a POSITIVE
CONTROL that passes and must keep passing. Nothing here migrates anything.

THE DEFECT
==========
The two stores have opposite shapes and the migration silently joins them:

  * ``state.db`` is PER-AGENT. Every agent gets its own SQLite file, so
    ``list_dispatches()`` with no filters means "my dispatches" — the SHARD
    does the scoping, and no code had to.
  * ``SCITEX_STORE_DSN`` is FLEET-WIDE. ``runtimes/_fleet_env.py`` injects one
    value — ``postgresql://scitex-primary:55432/scitex`` — into every
    container, and ``scitex_dev.store`` resolves it first. One endpoint, one
    ``dispatches_rows`` table, every agent writing into it.

Port ``dispatch_ledger`` as-is and 130+ per-agent shards become ONE shared
table with no ownership column. ``list_dispatches()`` then returns the WHOLE
FLEET's outbound traffic to whoever asks, and ``list_unreacted_dispatches()``
reports every other agent's comm-misses as this agent's own. NOTHING RAISES.
No column is missing, no query errors, no store is unreachable — the answers
just quietly become wrong, and wrong in the direction of "there is much more
traffic than I sent", which reads as data rather than as a bug.

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
belongs to nobody and is returned to everybody. So the fix cannot be "filter
on ``from_agent``" — the field is the wrong thing and is allowed to be absent.

THE SHAPE TO COPY ALREADY EXISTS
================================
``_state/inbound_ledger.py`` — the receiver-side mirror of this same ledger —
hit this exact problem when it moved (the FOURTH table to move) and solved it
by carrying the owner in the store IDENTITY: ``agent`` is the first of its
``IDENTITY_FIELDS = ("agent", "from_agent", "dispatch_id", "ts")``, it is
``required=True`` / ``MergeRule.IMMUTABLE``, ``record_inbound`` refuses an
empty one, and its production reader ``claim_oldest_pending(*, agent)`` cannot
be called without it. That is the shape these tests ask ``dispatch_ledger``
for: an OWNING AGENT, distinct from ``from_agent``, that the read surface can
scope on. The kwarg is spelled ``agent`` here for that reason and no other.

WHY ONE SHARED state.db IS THE HONEST FIXTURE
=============================================
The ``db_path`` fixture points BOTH agents at a SINGLE ``state.db``. That is
not an artificial cruelty — it is precisely the condition the migration
creates, expressed in the backend the module still uses. Giving each agent its
own file would reproduce the shard, and the shard is the thing being taken
away; a test that keeps it can only ever confirm that separate files stay
separate, which nobody doubts and which the migration deletes.

Because the module resolves its own store, these test bodies survive the port
unchanged: after the move the same calls address the shared PostgreSQL store
and must still answer with one agent's rows.

WHAT THESE TESTS DO NOT PROVE
=============================
They do not prove a leak is happening in production today. Measured across
``src/`` before writing them: ``list_dispatches`` and
``list_unreacted_dispatches`` have ZERO production callers — they are the
documented recall / comm-miss READ surface (``_network/peer.py`` names
``list_dispatches(to_agent=...)`` in prose) exercised by tests. The leak is
LATENT: it arrives with the port, not before it. That is the argument for
pinning it now rather than after, and it is also the reason this is a blocker
and not an incident.

They also say nothing about the WRITE path. ``record_dispatch`` /
``update_dispatch_status`` key on a uuid4 ``dispatch_id``, which stays unique
across a shared table, so writes do not collide. Only the reads leak.

HOW THE RED IS EXPECTED TO READ, AND THE ONE THING IT DOES NOT SHOW
==================================================================
All three failing tests stop at the SAME place — the ARRANGE — with
``TypeError: record_dispatch() got an unexpected keyword argument 'agent'``.
They never reach the read they are named after, and that is not a flaw in
them: it is the finding stated as sharply as it can be. THE LEAK CANNOT BE
WRITTEN DOWN AS A TEST, because the field a test would need to attribute a row
to its owner does not exist. A ledger that cannot record whose a dispatch is
cannot be asked to return only mine.

That does leave one thing unproven by the red alone — whether the failure is
about the missing field or about a fixture that never wrote anything — so
``test_one_shared_store_returns_both_agents_dispatches`` below is a POSITIVE
CONTROL. It uses only today's API, PASSES, and establishes that both agents'
rows really do land in one store and that the recall surface really does hand
back both. That passing test IS the leak, visible: the only thing separating
the two agents in production today is the FILE, and the migration deletes the
file.

Conventions, matching the two ``test_dispatch_ledger*`` modules beside this
one: AAA markers (STX-TQ002), one assertion per test (STX-TQ007), no mocks and
no monkeypatch (PA-306 / STX-NM002) — real sqlite under ``tmp_path``, isolated
through the same explicit ``os.environ`` save/restore fixture.
"""

from __future__ import annotations

import importlib
import os
import time
from pathlib import Path

import pytest

#: One agent's identity, and another's. Two names, ONE store — the whole point.
AGENT_A = "agent-alpha"
AGENT_B = "agent-beta"

#: Comfortably older than the SLO used below, so a recorded row is already
#: "stale" the moment it lands and ``list_unreacted_dispatches`` considers it.
_SLO_S = 30.0
_WELL_PAST_SLO_S = 600.0


@pytest.fixture
def shared_state_db(tmp_path: Path):
    """ONE ``state.db`` for every agent in the test — the post-migration shape.

    Named ``shared_state_db`` rather than ``db_path`` deliberately: the
    sibling modules' fixture of that name is read as "an isolated database",
    and the sharing is the entire experiment here. A future reader who copies
    it must see what they are copying.

    Explicit env save/restore, never ``monkeypatch`` (PA-306): the property
    under test is that the REAL resolver reads the REAL variable.
    """
    path = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(path)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield path
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — the fixture works, and the leak is already visible.
# ---------------------------------------------------------------------------


def test_one_shared_store_returns_both_agents_dispatches(shared_state_db: Path):
    """This one PASSES, and it is the reason the other three can be trusted.

    Two things a reader cannot otherwise tell apart:

      * the three failing tests below are red because the OWNING-AGENT FIELD
        IS MISSING, not because ``shared_state_db`` silently wrote nothing.
        A control that fails would mean the fixture, not the ledger.
      * the leak is not hypothetical. Two agents' sends land in one ledger and
        the recall surface returns BOTH — using only today's API, with no new
        parameter and no migration. Nothing here is broken yet ONLY because
        each agent has its own ``state.db`` file in production. The file is
        precisely what ``SCITEX_STORE_DSN`` removes.

    Written with today's spellings on purpose. It must keep passing after the
    owning-agent field lands: an UNFILTERED read over a shared ledger legally
    sees the whole ledger — that is what ``list_inbound(agent=None)`` does in
    the already-migrated mirror module. The fix is not to make the unfiltered
    read lie; it is to give callers a way to ask for their own.
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


def test_list_dispatches_returns_only_the_owning_agents_rows(shared_state_db: Path):
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
    shared_state_db: Path,
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


def test_an_anonymous_dispatch_does_not_leak_to_another_agent(shared_state_db: Path):
    """``from_agent=None`` is supported, so ownership must live elsewhere.

    Alpha records the supported anonymous row — the "script driving post_turn
    outside an agent container" case, which ``test_record_dispatch_allows_null_agents``
    beside this file pins as legal. Beta then asks for its own dispatches.

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
