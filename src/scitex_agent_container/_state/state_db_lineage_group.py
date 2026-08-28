"""The lineage WRITER and the group it derives — on PostgreSQL.

Extracted from :mod:`.state_db_nodes` on 2026-08-28, when ``lineage`` moved
off SQLite and the store-shaped writer no longer fit under that module's
line cap. It is the same split, for the same reason, that already gave
:mod:`.state_db_lineage_rel` its own file: ``state_db_nodes`` is the import
surface, not the implementation.

The pair here is the whole default-ACL mechanism. :func:`record_lineage`
writes ONE parent → child edge, at spawn time, from the two chokepoints
every spawn funnels through (:mod:`.._lifecycle._spawn_gate` locally,
:mod:`.._listen._agent_exec` for a brokered one). :func:`derive_group`
turns those edges into "who may talk to whom by default". Everything else
in the ACL reads one of the two.

Re-exported from :mod:`.state_db_nodes`, so every existing
``from ..._state.state_db_nodes import record_lineage`` keeps resolving.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .state_db_acl_policy import read_comms_policy

_logger = logging.getLogger(__name__)

__all__ = ["derive_group", "record_lineage"]


def record_lineage(
    *,
    child: str,
    parent: str,
) -> None:
    """Record ``parent`` as ``child``'s parent (keep-first-parent).

    Idempotent; a child's parent is set once and immutable. A DIFFERENT
    parent KEEPS the existing one (logged, NEVER raised) so a restart by a
    non-original-parent caller works in-place without re-parenting;
    identity drift stays impossible. Permission is gated upstream by
    ``check_spawn``.

    ON POSTGRESQL SINCE 2026-08-28, AND THE CONTRACT SURVIVED THE MOVE
    ==================================================================
    ``parent_name`` is IMMUTABLE in the store, which is the same rule this
    function has always enforced by hand — with one difference worth
    stating, because getting it wrong would turn the loud case silent:
    **IMMUTABLE keeps the first value and does NOT raise.** A differing
    write comes back in ``PutResult.conflicts`` as a ``MergeConflict``
    carrying ``kept`` / ``rejected`` / ``reason``. So the read-then-warn
    branch below is not the whole story, and the result of the ``put`` IS
    inspected rather than discarded: that is the path a concurrent writer
    or a replicated peer op takes, and it is exactly the case a "keeps
    first" contract exists to report.

    Both values stay in the oplog either way, so the contradiction is
    recoverable rather than merely logged.

    ``db_path`` is GONE. It named a SQLite file; there is no file.
    """
    if not child or not parent:
        raise ValueError("record_lineage: child and parent must be non-empty")

    from scitex_dev.store import ANY_REVISION

    from .state_db_lineage_store import ACTOR, run_with_reconnect

    def _write(store: Any) -> None:
        # ``include_hidden`` so a RETIRED edge is seen rather than written
        # over blind. The rename flow hides a child's old identity; a later
        # spawn reusing that name must not land a live-looking record on
        # top of a withdrawn one without anybody noticing.
        existing = store.get({"child_name": child}, include_hidden=True)
        if existing is not None:
            stored = str(existing.values["parent_name"])
            if stored != parent:
                _logger.warning(
                    "record_lineage: child %r keeps parent %r "
                    "(ignored re-parent to %r)",
                    child,
                    stored,
                    parent,
                )
                return
            if existing.hidden:
                # Same parent, same child, previously retired: this name is
                # genuinely back. Unhiding restores the edge WITH its
                # history rather than writing a second one. Only ever done
                # when the parent AGREES — resurrecting an edge whose
                # parent is contradicted would be the silent privilege
                # change the IMMUTABLE rule exists to prevent.
                store.unhide(
                    {"child_name": child},
                    expected_revision=ANY_REVISION,
                    actor=ACTOR,
                )
            return  # idempotent no-op
        result = store.put(
            {
                "child_name": child,
                "parent_name": parent,
                "created_at": time.time(),
            },
            expected_revision=ANY_REVISION,
            actor=ACTOR,
        )
        _log_lineage_conflicts(result, child=child, parent=parent)

    run_with_reconnect(_write)


def _log_lineage_conflicts(result: Any, *, child: str, parent: str) -> None:
    """Report what an IMMUTABLE field refused, at the severity it deserves.

    ``parent_name`` is the privilege-bearing one: a rejected value means two
    writers disagree about who spawned ``child``, and the ACL derives group
    membership from the answer. It gets a WARNING naming both values.

    ``created_at`` losing is ordinary: it can only differ when two writers
    raced on the same edge, and the winner is whichever got there first,
    which is the correct historical answer. Logged at debug, so the record
    exists without dressing a race up as a contradiction.
    """
    for conflict in getattr(result, "conflicts", ()):
        if conflict.field == "parent_name":
            _logger.warning(
                "record_lineage: child %r keeps parent %r (store rejected %r "
                "as a second, differing claim; both remain in the oplog)",
                child,
                conflict.kept,
                conflict.rejected,
            )
        else:
            _logger.debug(
                "record_lineage: child %r kept %s=%r over %r (concurrent "
                "write of the same edge; parent %r agreed)",
                child,
                conflict.field,
                conflict.kept,
                conflict.rejected,
                parent,
            )


def derive_group(
    *,
    name: str,
) -> set[str]:
    """Return the set of nodes inside ``name``'s default-ACL group.

    A *group* is a parent together with its direct children
    (handoff §2). Concretely:

    * If ``name`` is a parent (any edge with ``parent_name = name``):
      group = {name} ∪ {its direct children}.
    * If ``name`` is a child (an edge with ``child_name = name``):
      group = {its parent} ∪ {parent's other children}.
    * If ``name`` has no edges at all: group = {name} (singleton —
      a fresh registration starts unattached).

    The derivation is intentionally local — it never walks the full
    lineage tree. That keeps the default-ACL semantics simple and
    matches handoff §2: "the group is the unit of default ACL" (one
    parent + its direct children, not the entire ancestry).

    Phase-3 (ADR-0010 Step 2): if ``name``'s ``node_comms_policy`` row
    sets ``lineage_group = 'solitary'``, the group is forced to
    ``{name}`` and the lineage walk is skipped. That isolates a capsule
    from its siblings AND its parent without depending on the lineage
    table being empty — clew capsule children adopt this so a sibling
    capsule can never address them through the group-default ACL even
    though they share a parent edge.

    THE SOLITARY SHORT-CIRCUIT STILL RETURNS BEFORE THE STORE IS TOUCHED,
    and that is now load-bearing in a way it was not against SQLite. The
    lineage import sits BELOW the branch on purpose: a solitary capsule
    must resolve its own singleton group without a PostgreSQL round-trip,
    so an unreachable primary cannot turn "isolated" into an exception on
    a path that never needed an edge in the first place.
    """
    if not name:
        raise ValueError("derive_group: name must be non-empty")
    # Phase-3 solitary override — short-circuits to the singleton group
    # without touching the lineage store. Keep the import below this line.
    policy = read_comms_policy(name=name)
    if policy["lineage_group"] == "solitary":
        return {name}

    from .state_db_lineage_store import read_edges

    edges = read_edges()
    children = edges.children(name)
    if children:
        return {name} | children
    parent = edges.parent(name)
    if parent is None:
        return {name}
    return {parent} | edges.children(parent)

# EOF
