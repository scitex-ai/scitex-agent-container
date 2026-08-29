"""Lineage walk helpers (PR-3 — transitive descendants and ancestors).

Extracted from :mod:`._state.state_db_nodes` (which hit the per-file line
cap) so the PR-3 lineage-scoped ACL gate has a focused module to import
from. The edges themselves (``child_name``, ``parent_name``,
``created_at``) are written by
:func:`._state.state_db_lineage_group.record_lineage`; this module is
read-only.

ON POSTGRESQL SINCE 2026-08-28. ``db_path`` is gone from both signatures —
it named a SQLite file and there is no file. What changed for the walks
themselves is the number of round-trips, and it went DOWN: each function
now reads the edge set ONCE through
:func:`.state_db_lineage_store.read_edges` and walks it in memory, where
:func:`descendants_of` previously issued one ``SELECT ... WHERE parent_name
IN (...)`` PER BFS LEVEL and :func:`ancestors_to_root` one per ancestor.
The cycle guards are unchanged and still do the work described below —
they guard the WALK, not the storage, so a contradictory edge set is
bounded here whatever produced it.

The PR-3 lineage-scoped ACL contract (clew checkpoint 3):

  caller may operate on agent ``target`` iff
      caller is None (admin / operator path)            OR
      caller == target (self-management)                OR
      target in descendants_of(caller)  (lineage scope)

The first two conditions are answered by the listen-side ACL
helpers in :mod:`._listen._acl`; this module provides the third
(transitive lineage walk).
"""

from __future__ import annotations

__all__ = ["ancestors_to_root", "descendants_of"]


def descendants_of(
    *,
    name: str,
    max_depth: int = 64,
) -> set[str]:
    """Return the set of transitive descendants of ``name``.

    Used by the PR-3 :func:`~.._listen._acl.check_lineage_acl`
    gate to answer "may caller operate on target?" — the answer
    is yes when ``target ∈ descendants(caller)`` (transitively,
    not just direct children).

    The walk is breadth-first over the lineage edges; the return set does
    NOT include ``name`` itself (callers checking self-management should do
    so before calling this). Cycles (which
    :func:`~.state_db_lineage_group.record_lineage` prevents, but which a
    contradictory edge set could still contain) are guarded by both the
    seen set AND a depth ceiling — a runaway walk is bounded to
    ``max_depth`` levels deep.

    The default ``max_depth=64`` is deeper than any realistic SAC
    deployment (cohort sizes top out at ~50 capsules; a 64-deep
    chain would be a degenerate tree) and is here purely to
    prevent a malformed edge set from infinitely looping the listen
    server.

    A non-existent ``name`` returns the empty set (nothing has it
    as a parent) — same shape as a leaf node, so callers don't
    need to disambiguate.

    ONE BEHAVIOUR CHANGED IN THE CYCLE CASE, stated rather than left to
    be discovered. The SQLite version guarded only on the ``seen`` set, so
    given the edges ``a → b`` and ``b → a`` it returned ``{b, a}`` —
    including ``a`` itself, which contradicts the "does NOT include
    ``name``" promise three paragraphs up. The ``child == name`` guard
    below makes the code keep that promise. Nothing downstream shifts:
    :func:`~.._listen._acl.check_lineage_acl` answers ``caller == target``
    before it ever walks, so self was never reachable through this set,
    and a cycle is a malformed edge set in the first place.

    Args:
        name: the agent whose descendants we want.
        max_depth: BFS depth ceiling. Default 64 (= safety bound,
            never reached in practice).

    Returns:
        Set of descendant agent names (not including ``name``).
    """
    if not name:
        return set()

    from .state_db_lineage_store import read_edges

    edges = read_edges()
    out: set[str] = set()
    # BFS by levels so ``max_depth`` is a real DEPTH bound rather than a
    # queue-size bound. ``frontier`` carries the current depth's nodes.
    frontier = {name}
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: set[str] = set()
        for parent in frontier:
            for child in edges.children(parent):
                if child in out or child == name:
                    # Already seen — cycle guard. ``record_lineage`` never
                    # produces one, but the listen server cannot trust the
                    # edge set blindly, and ``child == name`` catches the
                    # shortest cycle of all (a node reaching itself).
                    continue
                out.add(child)
                next_frontier.add(child)
        frontier = next_frontier
        depth += 1
    return out


def ancestors_to_root(
    *,
    name: str,
    max_depth: int = 64,
) -> list[str]:
    """Return the lineage chain from ``name``'s parent up to the root.

    The UP walk complementing :func:`descendants_of`. Used by the
    CI-feedback ring (feedback.pdf §3) to climb pusher → parent → … →
    lead when delivering a verdict up the recorded lineage.

    Ordered immediate-parent first, root (the topmost ancestor with no
    parent) last. Does NOT include ``name`` itself. A node with no
    parent — or an unknown ``name`` — returns ``[]``.

    Cycle guard: a ``seen`` set plus the ``max_depth`` ceiling bound the
    walk so a parent cycle (which
    :func:`~.state_db_lineage_group.record_lineage` never produces) cannot
    loop the listen server — same rationale as :func:`descendants_of`.

    Args:
        name: the agent whose ancestor chain we want (the pusher).
        max_depth: walk depth ceiling. Default 64 (safety bound).

    Returns:
        Ordered list of ancestor agent names (parent first, root last),
        not including ``name``.
    """
    if not name:
        return []

    from .state_db_lineage_store import read_edges

    edges = read_edges()
    chain: list[str] = []
    seen: set[str] = {name}
    current = name
    depth = 0
    while depth < max_depth:
        parent = edges.parent(current)
        if parent is None:
            break
        if parent in seen:
            break  # cycle guard; record_lineage never loops
        chain.append(parent)
        seen.add(parent)
        current = parent
        depth += 1
    return chain

# EOF
