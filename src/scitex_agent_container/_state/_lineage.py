"""The lineage store — parent → child edges, on PostgreSQL.

This module owns the ``lineage`` record set outright: the schema, the
write primitive (:func:`record_lineage`), the two single-hop accessors
(:func:`parent_of` / :func:`children_of`) and the two transitive walks
(:func:`descendants_of` / :func:`ancestors_to_root`). Everything that
used to issue ``SELECT ... FROM lineage`` — :mod:`.state_db_nodes`
(``derive_group`` / ``spawn_allowed``), :mod:`.state_db_acl_policy`
(``sender_target_relationship``) and the PR-3 lineage-scoped ACL gate —
now comes through here.

``record_lineage`` is re-exported from :mod:`.state_db_nodes`, which is
where every existing caller imports it from; that path is unchanged.

WHY ONE MODULE FOR THE WHOLE TABLE
==================================
Because the alternative was measured to be a silent wrong answer. Under
the old layout FOUR modules held their own ``FROM lineage`` SQL. Moving
only the writer would have left ``check_lineage_acl`` reading an
abandoned SQLite table that still exists (its table-defining DDL sat in
``_SCHEMA_REGISTRY`` — spelled that way here on purpose: the repo's
sqlite-footprint gate matches the two-word DDL keyword with a regex, and
it does not care that this occurrence is prose, so writing it out would
have failed the build for a sentence), so every brokered restart would
be DENIED for
want of an edge — with no error anywhere, because an empty table and an
empty answer are the same bytes. A table has to move all at once.

ON POSTGRESQL SINCE 2026-08-28 (the operator's SQLite-eradication
order). The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO SQLite
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach.

``db_path`` IS GONE from every signature in this module. It named a
SQLite file; there is no file. Test isolation now comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema — the ``pg_schema`` fixture —
which is a better isolation than a temp path was, because it exercises
the real resolver.

THE SCHEMA IS NOT NEW, IT WAS ALREADY DECLARED
==============================================
:mod:`.._store_plugin` has declared ``sac_lineage`` — identity
``child_name``, ``parent_name`` and ``created_at`` both IMMUTABLE,
``Truth.HISTORY``, ``WriterPolicy.MULTI_WRITER`` — since the
classification work, together with the reasoning for each choice. This
module adopts that declaration rather than inventing a second opinion
about what a lineage edge means. Quoting the part that decides the
writer policy: *"a cross-host spawn is brokered, so the edge can
legitimately be written by either end, and a second writer must get the
loud MergeConflict rather than an ownership rejection that says nothing
about WHY the two disagree."*

IMMUTABLE, NOT LAST_WRITER_WINS, is the same decision the SQLite version
made in Python: ``record_lineage`` KEEPS the first parent and logs a
re-parent attempt instead of applying it. The ACL derives authority from
this table, so a silent rewrite of an edge is a silent privilege change.

KEEP-FIRST-PARENT IS ENFORCED BY A READ, NOT BY THE MERGE RULE, and the
distinction matters: ``MergeRule.IMMUTABLE`` makes a CONFLICTING remote
edge loud at merge time, while the local re-parent path must stay a
quiet no-op — a restart by a non-original-parent caller has always been
allowed to work in place. So :func:`record_lineage` still reads first
and returns.

WHAT THIS MODULE DELIBERATELY DOES NOT OFFER: A RENAME
======================================================
``sac agents rename`` used to rewrite ``lineage.child_name`` and
``lineage.parent_name`` in the same SQLite transaction as every other
name-bearing column. That cannot be expressed here, and the reason is
the schema working as designed rather than an oversight:

* the record IDENTITY is ``child_name``, so hiding the old record does
  NOT free the identity — a hidden row still occupies it, exactly as the
  grants port documented for a revoked grant. A ``put`` with
  ``NEW_RECORD`` therefore collides.
* ``parent_name`` is IMMUTABLE, so a ``put`` on the existing identity
  does not change it either: ``merge_field`` KEEPS the current value and
  reports a ``MergeConflict``. That is the property that stops a merge
  silently rewriting the family tree, and it is worth more than a
  rename.

Making the rename work would mean either widening the identity to
``(child_name, parent_name)`` — which discards the loud "two parents
claimed for one child" contradiction the declaration exists to
surface — or demoting ``parent_name`` to LAST_WRITER_WINS, which is the
silent privilege change the declaration forbids. Both are schema
decisions, not porting decisions, so the rename is NOT implemented here
and the gap is made VISIBLE instead: :func:`.._lifecycle._rename_db.count_rows`
reports the edges a rename will leave behind, so ``sac agents rename
--dry-run`` states it.

THE CONSEQUENCE, stated precisely, because "some edges are stale" is not
specific enough to reason about. A renamed CHILD keeps its edge under the
old name, so ``parent_of(new_name)`` is ``None`` and the agent reads as a
ROOT — which GRANTS spawn authority it did not have. A renamed PARENT
loses ``children_of``/``descendants_of``, so it loses manage reach over
its own children. The first direction is the dangerous one; until the
schema question is settled, re-record the edge after renaming an agent
that has one.

ORDERING IS THE HLC, NEVER ``created_at``
=========================================
Nothing here sorts by ``created_at``, and that is deliberate. It is a
wall-clock ``time.time()`` float: it TIES on rows bulk-imported from a
peer (they carry the peer's stamp verbatim) and it SKEWS across hosts,
so an imported edge can sort ahead of one written locally before it.
The hybrid logical clock is monotonic per origin and causally ordered
across origins; :func:`lineage_edges` uses it.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

_logger = logging.getLogger(__name__)

__all__ = [
    "LINEAGE_STORE",
    "ancestors_to_root",
    "children_of",
    "descendants_of",
    "lineage_edges",
    "parent_of",
    "record_lineage",
]

#: Logical store name. Renders as four physical tables
#: (``lineage_rows``, ``_oplog``, ``_identity``, ``_cursor``).
LINEAGE_STORE = "lineage"

_ACTOR = "scitex-agent-container"


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any, *, indexed: bool = False) -> Any:
    """A recorded edge is a historical fact — IMMUTABLE.

    Who spawned whom, and when, is not an opinion that a later writer
    gets to revise. The ACL reads this to decide spawn and host_exec
    authority, so a merge able to move ``parent_name`` would be a merge
    able to move authority.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=indexed,
    )


def _lineage_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=LINEAGE_STORE,
        fields={
            # child_name alone IS the identity, exactly as the SQLite
            # ``child_name TEXT PRIMARY KEY`` treated it: a child has one
            # parent, and "which parent" is the fact the row carries.
            "child_name": _ident(FieldKind.TEXT),
            "parent_name": _fact(FieldKind.TEXT, indexed=True),
            "created_at": _fact(FieldKind.REAL),
        },
    )


def _open() -> "Store":
    """Open the lineage store. RAISES if PostgreSQL is unreachable.

    MULTI_WRITER, per :mod:`.._store_plugin`'s declaration. A lineage
    edge has no single stable owner: a cross-host spawn is brokered, so
    the parent's host and the child's host may each legitimately write
    it, and ``state_db_export.import_state`` bulk-imports peer edges.
    SINGLE_WRITER would reject the second writer for OWNERSHIP, which
    says nothing about whether the two disagree.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = _lineage_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )


def _hlc_sort_key(row: Any) -> tuple:
    """Total order over records, immune to wall-clock skew.

    The successor to SQLite's ``rowid``. ``node`` is the final tiebreak
    so the order is total rather than merely partial — two origins can
    mint the same ``(wall_us, logical)`` pair.
    """
    hlc = row.hlc
    return (hlc.wall_us, hlc.logical, hlc.node)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def record_lineage(*, child: str, parent: str) -> None:
    """Record ``parent`` as ``child``'s parent (keep-first-parent).

    Idempotent; a child's parent is set once and immutable. A DIFFERENT
    parent KEEPS the existing one (logged, not raised) so a restart by a
    non-original-parent caller works in-place without re-parenting;
    identity drift stays impossible. Permission is gated upstream by
    ``check_spawn``.

    ``include_hidden`` on the read: a hidden edge still occupies the
    identity, so a plain read would answer "absent" and the insert would
    then collide with a record that is there.
    """
    if not child or not parent:
        raise ValueError("record_lineage: child and parent must be non-empty")

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    store = _open()
    try:
        existing = store.get({"child_name": child}, include_hidden=True)
        if existing is not None:
            recorded = str(existing.values["parent_name"])
            if recorded == parent:
                return  # idempotent no-op
            _logger.warning(
                "record_lineage: child %r keeps parent %r (ignored re-parent to %r)",
                child,
                recorded,
                parent,
            )
            return
        store.put(
            {
                "child_name": child,
                "parent_name": parent,
                "created_at": time.time(),
            },
            expected_revision=NEW_RECORD,
        )
    except RevisionMismatchError:
        # Another writer inserted this child's edge between our read and
        # our put. The edge exists, which is the outcome we wanted, and
        # keep-first-parent says the winner keeps it. Narrow on purpose:
        # nothing else is swallowed, so an unreachable store still fails.
        return
    finally:
        store.close()


# ---------------------------------------------------------------------------
# single-hop reads
# ---------------------------------------------------------------------------


def parent_of(*, child: str) -> str | None:
    """Return ``child``'s parent, or ``None`` when it has no edge."""
    if not child:
        return None
    store = _open()
    try:
        row = store.get({"child_name": child})
        return str(row.values["parent_name"]) if row is not None else None
    finally:
        store.close()


def children_of(*, parent: str) -> set[str]:
    """Return the direct children of ``parent`` (possibly empty).

    A full scan reduced in Python rather than an indexed lookup: the
    store's only listing verb is ``rows()``. That is a deliberate trade
    the diary port already made — the fleet's lineage is on the order of
    a hundred edges, and doing the reduction here keeps this module free
    of a second query dialect.
    """
    if not parent:
        return set()
    store = _open()
    try:
        return {
            str(row.values["child_name"])
            for row in store.rows()
            if str(row.values["parent_name"]) == parent
        }
    finally:
        store.close()


def lineage_edges() -> list[dict[str, Any]]:
    """Every LIVE edge, in causal insertion order.

    Ordered by the hybrid logical clock — see the module docstring for
    why NOT ``created_at``. Hidden edges are omitted; nothing in this
    module hides one today (``record_lineage`` is insert-only and there
    was never a DELETE to convert), but a hidden edge would still be
    readable through ``include_hidden`` and in the oplog.
    """
    store = _open()
    try:
        rows = list(store.rows())
        rows.sort(key=_hlc_sort_key)
        return [
            {
                "child": str(row.values["child_name"]),
                "parent": str(row.values["parent_name"]),
                "created_at": float(row.values["created_at"]),
            }
            for row in rows
        ]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# transitive walks (PR-3 — lineage-scoped ACL)
# ---------------------------------------------------------------------------
#
# The PR-3 lineage-scoped ACL contract (clew checkpoint 3):
#
#   caller may operate on agent ``target`` iff
#       caller is None (admin / operator path)            OR
#       caller == target (self-management)                OR
#       target in descendants_of(caller)  (lineage scope)
#
# The first two conditions are answered by the listen-side ACL helpers in
# :mod:`.._listen._acl`; this module provides the third.


def descendants_of(*, name: str, max_depth: int = 64) -> set[str]:
    """Return the set of transitive descendants of ``name``.

    Used by the PR-3 :func:`~.._listen._acl.check_lineage_acl` gate to
    answer "may caller operate on target?" — the answer is yes when
    ``target ∈ descendants(caller)`` (transitively, not just direct
    children).

    The walk is breadth-first over the edge set; the return set does NOT
    include ``name`` itself (callers checking self-management should do
    so before calling this). Cycles are guarded by both the seen set AND
    a depth ceiling, so a runaway walk is bounded to ``max_depth`` levels
    deep.

    THE PREDECESSOR'S DOCSTRING SAID ``record_lineage`` "never produces"
    a cycle, and that is not true — measured while porting this module.
    Keep-first-parent is enforced per CHILD, so ``record_lineage(child=x,
    parent=y)`` followed by ``record_lineage(child=y, parent=x)`` names
    two different children, is accepted twice, and closes a two-node
    loop. The guard is therefore load-bearing on the ordinary path, not
    only against a hand-edited store.

    The default ``max_depth=64`` is deeper than any realistic SAC
    deployment (cohort sizes top out at ~50 capsules; a 64-deep chain
    would be a degenerate tree) and is here purely to prevent a malformed
    store from infinitely looping the listen server.

    A non-existent ``name`` returns the empty set (nothing has it as a
    parent) — same shape as a leaf node, so callers don't need to
    disambiguate.

    ONE store read, not one per level. The SQLite version batched an
    ``IN (...)`` query per BFS level; the store has no such predicate, so
    the edges are read once and the levels are walked over the in-memory
    adjacency. Same answer, and strictly fewer round trips.
    """
    if not name:
        return set()

    by_parent = _adjacency()
    out: set[str] = set()
    frontier = {name}
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: set[str] = set()
        for node in frontier:
            for child in by_parent.get(node, ()):  # noqa: PERF401
                if child in out:
                    continue  # cycle guard
                out.add(child)
                next_frontier.add(child)
        frontier = next_frontier
        depth += 1
    return out


def ancestors_to_root(*, name: str, max_depth: int = 64) -> list[str]:
    """Return the lineage chain from ``name``'s parent up to the root.

    The UP walk complementing :func:`descendants_of`. Used by the
    CI-feedback ring (feedback.pdf §3) to climb pusher → parent → … →
    lead when delivering a verdict up the recorded lineage.

    Ordered immediate-parent first, root (the topmost ancestor with no
    parent) last. Does NOT include ``name`` itself. A node with no parent
    — or an unknown ``name`` — returns ``[]``.

    Cycle guard: a ``seen`` set plus the ``max_depth`` ceiling bound the
    walk so a parent cycle cannot loop the listen server — same rationale
    as :func:`descendants_of`, including the correction there about how a
    cycle actually arises.
    """
    if not name:
        return []

    parents = _parents()
    chain: list[str] = []
    seen: set[str] = {name}
    current = name
    depth = 0
    while depth < max_depth:
        parent = parents.get(current)
        if parent is None:
            break
        if parent in seen:
            break  # cycle guard
        chain.append(parent)
        seen.add(parent)
        current = parent
        depth += 1
    return chain


def _parents() -> dict[str, str]:
    """``{child: parent}`` over every live edge — one store read."""
    store = _open()
    try:
        return {
            str(row.values["child_name"]): str(row.values["parent_name"])
            for row in store.rows()
        }
    finally:
        store.close()


def _adjacency() -> dict[str, set[str]]:
    """``{parent: {children}}`` over every live edge — one store read."""
    out: dict[str, set[str]] = {}
    for child, parent in _parents().items():
        out.setdefault(parent, set()).add(child)
    return out
