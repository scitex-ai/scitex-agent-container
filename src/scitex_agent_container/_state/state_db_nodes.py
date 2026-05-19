"""Lineage + ACL-grant primitives for WI-2 ACL (limited scope).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-20 directive (limited scope — defer authenticated identity):

  * Group-based default ACL — intra-group bidirectional, cross-group
    denied. Group is *derived from lineage*; see
    :func:`derive_group`.

  * Cross-group grants — accepted, keyed on the self-claimed
    ``metadata.from_agent`` field, **with the caveat that each row
    "trusts metadata.from_agent until per-node creds land"**. The
    cryptographic-identity follow-on is tracked in scitex-lead at
    ``GITIGNORED/FUTURE/sac-per-node-authenticated-acl.md``.

  * Spawn permission — root-only by current policy (a node with no
    parent may call ``sac agents start``; a child may not). The
    policy is **lift-able**: lifting it later is a single-callsite
    edit to :func:`spawn_allowed` with zero schema change (handoff
    §2 D5 "Depth limit is a POLICY, not a structural ceiling").

The N-level structural capability of ``lineage`` is preserved —
nothing here hard-codes "2" or assumes fixed depth.

All times stored as ``REAL`` unix-seconds (float).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

__all__ = [
    "derive_group",
    "grant_send",
    "has_grant",
    "is_local_node",
    "list_comms_grants",
    "record_lineage",
    "resolve_node_host",
    "revoke_send",
    "spawn_allowed",
]


# ---------------------------------------------------------------------------
# lineage — parent → child edges and the group they imply
# ---------------------------------------------------------------------------


def record_lineage(
    *,
    child: str,
    parent: str,
    db_path: Path | None = None,
) -> None:
    """Record ``parent`` as the parent of ``child``.

    Idempotent — a second call with the same child+parent leaves the
    row untouched. A different parent for an existing child raises
    ``ValueError`` (re-parenting is not a quiet operation; a child
    that "switches groups" is exactly the kind of identity drift the
    ACL is meant to prevent).
    """
    if not child or not parent:
        raise ValueError("record_lineage: child and parent must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (child,)
        ).fetchone()
        if existing is not None:
            if existing["parent_name"] == parent:
                return  # idempotent no-op
            raise ValueError(
                f"record_lineage: child {child!r} already has parent "
                f"{existing['parent_name']!r}; refusing to re-parent to "
                f"{parent!r}"
            )
        conn.execute(
            "INSERT INTO lineage (child_name, parent_name, created_at) "
            "VALUES (?, ?, ?)",
            (child, parent, time.time()),
        )


def derive_group(
    *,
    name: str,
    db_path: Path | None = None,
) -> set[str]:
    """Return the set of nodes inside ``name``'s default-ACL group.

    A *group* is a parent together with its direct children
    (handoff §2). Concretely:

    * If ``name`` is a parent (any rows in ``lineage`` with
      ``parent_name = name``): group = {name} ∪ {its direct children}.
    * If ``name`` is a child (row in ``lineage`` with
      ``child_name = name``): group = {its parent} ∪ {parent's other
      children}.
    * If ``name`` has no edges at all: group = {name} (singleton —
      a fresh registration starts unattached).

    The derivation is intentionally local — it never walks the full
    lineage tree. That keeps the default-ACL semantics simple and
    matches handoff §2: "the group is the unit of default ACL" (one
    parent + its direct children, not the entire ancestry).
    """
    if not name:
        raise ValueError("derive_group: name must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        children_rows = conn.execute(
            "SELECT child_name FROM lineage WHERE parent_name = ?", (name,)
        ).fetchall()
        if children_rows:
            group: set[str] = {name}
            for r in children_rows:
                group.add(str(r["child_name"]))
            return group
        parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (name,)
        ).fetchone()
        if parent_row is None:
            return {name}
        parent = str(parent_row["parent_name"])
        sibling_rows = conn.execute(
            "SELECT child_name FROM lineage WHERE parent_name = ?", (parent,)
        ).fetchall()
        group = {parent}
        for r in sibling_rows:
            group.add(str(r["child_name"]))
        return group


# ---------------------------------------------------------------------------
# spawn permission — current policy: root-only spawn
# ---------------------------------------------------------------------------


def spawn_allowed(
    *,
    caller: str | None,
    db_path: Path | None = None,
) -> tuple[bool, str | None]:
    """Decide whether ``caller`` is allowed to call ``sac agents start``.

    Current policy (handoff §4 / WI-2): a *root* node (no parent) is
    allowed to spawn; a child is not. ``caller=None`` means the
    administrative / human-operator path (e.g., a shell invocation
    from outside any sac-managed agent) — allowed.

    Returns ``(True, None)`` on allow or ``(False, reason)`` on
    deny. The reason is suitable for inclusion in a 403 body and a
    host log line.

    **Lift-able policy**: when N-level spawning becomes acceptable
    (handoff §2 D5), this function shrinks to ``return (True, None)``
    — zero schema change, zero data migration.
    """
    if caller is None or caller == "":
        # Admin / human operator. Allowed.
        return (True, None)
    from .state_db import open_db

    with open_db(db_path) as conn:
        parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (caller,)
        ).fetchone()
    if parent_row is None:
        return (True, None)  # caller has no parent → root → allow
    return (
        False,
        (
            f"spawn denied: caller {caller!r} is a child of "
            f"{parent_row['parent_name']!r}. Current policy allows only "
            "root nodes to spawn (handoff §4 'lift-able policy' — change "
            "is a single edit to spawn_allowed())."
        ),
    )


# ---------------------------------------------------------------------------
# comms_grants — explicit cross-group send permissions
# ---------------------------------------------------------------------------

# Standard audit caveat written into every grant row. The follow-on
# work item (per lead 2026-05-20) will close this gap.
GRANT_DEFERRED_CAVEAT = "trusts metadata.from_agent until per-node creds land"


def grant_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
    note: str | None = None,
) -> None:
    """Insert (or refresh) a cross-group grant ``sender → target``.

    Idempotent — re-granting the same pair leaves the row untouched
    (timestamp not bumped). The ``note`` defaults to the standard
    deferred-identity caveat so every grant carries the audit trail
    the follow-on cryptographic-identity work item will close.
    """
    if not sender or not target:
        raise ValueError("grant_send: sender and target must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM comms_grants "
            "WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comms_grants "
            "(sender_name, target_name, created_at, note) "
            "VALUES (?, ?, ?, ?)",
            (sender, target, time.time(), note or GRANT_DEFERRED_CAVEAT),
        )


def revoke_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Remove a ``sender → target`` grant. Returns ``True`` iff a row
    was removed."""
    if not sender or not target:
        return False
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM comms_grants "
            "WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        )
    return cur.rowcount > 0


def has_grant(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff a ``sender → target`` cross-group grant
    exists."""
    if not sender or not target:
        return False
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM comms_grants "
            "WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
    return row is not None


def list_comms_grants(
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every grant row in insertion order.

    Observability surface for the host operator. Each row carries the
    audit ``note`` (default: the deferred-identity caveat).
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT sender_name, target_name, created_at, note "
            "FROM comms_grants "
            "ORDER BY created_at ASC, sender_name ASC, target_name ASC"
        )
        return [
            {
                "sender": str(r["sender_name"]),
                "target": str(r["target_name"]),
                "created_at": float(r["created_at"]),
                "note": (r["note"] if r["note"] is not None else None),
            }
            for r in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# WI-4 — name → host resolver primitives. Kept here (rather than
# splitting into a sibling module) because the cross-host forwarder
# consults both the resolver and the ACL primitives from this module.
# ---------------------------------------------------------------------------


def resolve_node_host(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Map a node ``name`` to ``{host, a2a_port}`` from the
    ``instances`` table.

    Returns ``None`` when the name does not match a *live* instance
    (``ended_at IS NULL``). When several live records exist for the
    same name (e.g., a restart race), the most recently started one
    wins — cross-host forwarding cannot pick non-deterministically.
    """
    if not name:
        return None
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT host, a2a_port
              FROM instances
             WHERE name = ? AND ended_at IS NULL
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (name,),
        ).fetchone()
    if row is None:
        return None
    return {
        "host": str(row["host"]),
        "a2a_port": int(row["a2a_port"]) if row["a2a_port"] is not None else None,
    }


def is_local_node(
    *,
    name: str,
    local_host: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` if ``name`` should be served locally.

    Local cases:

    * The name resolves to ``local_host`` via :func:`resolve_node_host`.
    * The name does NOT resolve to any host (unknown / external node) —
      defer to the local ``NodeRegistry`` implicit-registration path.
      Forwarding a never-seen name would synthesise an SSRF target
      from a self-claimed string; the host-local path is correct.
    """
    info = resolve_node_host(name=name, db_path=db_path)
    if info is None:
        return True
    return info["host"] == local_host
