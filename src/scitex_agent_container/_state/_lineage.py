"""Lineage-table walk helpers (PR-3 — transitive descendants).

Extracted from :mod:`._state.state_db_nodes` (which hit the
per-file line cap) so the PR-3 lineage-scoped ACL gate has a
focused module to import from. The ``lineage`` table itself
(``child_name``, ``parent_name``, ``created_at``) is created and
mutated by :func:`._state.state_db_nodes.record_lineage`; this
module is read-only.

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

from pathlib import Path

__all__ = ["ancestors_to_root", "descendants_of"]


def descendants_of(
    *,
    name: str,
    db_path: Path | None = None,
    max_depth: int = 64,
) -> set[str]:
    """Return the set of transitive descendants of ``name``.

    Used by the PR-3 :func:`~.._listen._acl.check_lineage_acl`
    gate to answer "may caller operate on target?" — the answer
    is yes when ``target ∈ descendants(caller)`` (transitively,
    not just direct children).

    The walk is breadth-first over the ``lineage`` table; the
    return set does NOT include ``name`` itself (callers checking
    self-management should do so before calling this). Cycles in
    the lineage table (which :func:`record_lineage` prevents but
    a hand-edited DB could introduce) are guarded by both the
    seen set AND a depth ceiling — a runaway walk is bounded to
    ``max_depth`` levels deep.

    The default ``max_depth=64`` is deeper than any realistic SAC
    deployment (cohort sizes top out at ~50 capsules; a 64-deep
    chain would be a degenerate tree) and is here purely to
    prevent a malformed DB from infinitely looping the listen
    server.

    A non-existent ``name`` returns the empty set (nothing has it
    as a parent) — same shape as a leaf node, so callers don't
    need to disambiguate.

    Args:
        name: the agent whose descendants we want.
        db_path: optional override for the state.db path; tests
            pass a tmp file so the global DB stays clean.
        max_depth: BFS depth ceiling. Default 64 (= safety bound,
            never reached in practice).

    Returns:
        Set of descendant agent names (not including ``name``).
    """
    if not name:
        return set()
    from .state_db import open_db

    out: set[str] = set()
    with open_db(db_path) as conn:
        # BFS by levels so we hit max_depth as a real depth bound
        # rather than a queue-size bound. ``frontier`` carries the
        # current depth's nodes; we batch-query their children with
        # an IN clause to avoid one round-trip per node.
        frontier = {name}
        depth = 0
        while frontier and depth < max_depth:
            placeholders = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT child_name FROM lineage WHERE parent_name IN ({placeholders})",
                tuple(frontier),
            ).fetchall()
            next_frontier: set[str] = set()
            for r in rows:
                child = str(r["child_name"])
                if child in out:
                    # Already seen — cycle guard (a hand-edited DB
                    # could have it; record_lineage never produces
                    # one but the listen server cannot trust the DB
                    # blindly).
                    continue
                out.add(child)
                next_frontier.add(child)
            frontier = next_frontier
            depth += 1
    return out


def ancestors_to_root(
    *,
    name: str,
    db_path: Path | None = None,
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
    walk so a hand-edited DB with a parent cycle (which
    :func:`record_lineage` never produces) cannot loop the listen
    server — same rationale as :func:`descendants_of`.

    Args:
        name: the agent whose ancestor chain we want (the pusher).
        db_path: optional override for the state.db path; tests pass a
            tmp file so the global DB stays clean.
        max_depth: walk depth ceiling. Default 64 (safety bound).

    Returns:
        Ordered list of ancestor agent names (parent first, root last),
        not including ``name``.
    """
    if not name:
        return []
    from .state_db import open_db

    chain: list[str] = []
    seen: set[str] = {name}
    with open_db(db_path) as conn:
        current = name
        depth = 0
        while depth < max_depth:
            row = conn.execute(
                "SELECT parent_name FROM lineage WHERE child_name = ?",
                (current,),
            ).fetchone()
            if row is None:
                break
            parent = str(row["parent_name"])
            if parent in seen:
                break  # cycle guard (hand-edited DB; record_lineage never loops)
            chain.append(parent)
            seen.add(parent)
            current = parent
            depth += 1
    return chain
