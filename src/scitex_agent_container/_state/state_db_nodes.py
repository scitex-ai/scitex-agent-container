"""Node-identity primitives for WI-2 ACL.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-21 directive (RESTORED the authenticated-identity criterion
the prior limited scope had deferred):

  * **Authenticated identity** — per-node bearer tokens minted at
    registration (:func:`mint_node_token`). The listen server resolves
    an incoming ``Authorization: Bearer <token>`` to a node name
    (:func:`resolve_node_token`). With this in place,
    ``check_send_acl`` enforces "identity cannot be spoofed via a
    metadata field" — when a per-node bearer is presented,
    ``params.metadata.from_agent`` MUST match the bearer's resolved
    name; mismatch → 403.

  * **Group-based default ACL** — intra-group bidirectional,
    cross-group denied. Group is *derived from lineage*; see
    :func:`derive_group`.

  * **Cross-group grants** — accepted, keyed on the *resolved*
    sender identity (per-node bearer authenticates the sender; the
    host-wide bearer is the administrative / cross-host-forwarder
    path, which honours ``metadata.from_agent`` verbatim). See
    :func:`grant_send`.

  * **Spawn permission** — root-only by current policy (a node with
    no parent may call ``sac agents start``; a child may not). The
    policy is **lift-able**: lifting it later is a single-callsite
    edit to :func:`spawn_allowed` with zero schema change (handoff
    §2 D5 "Depth limit is a POLICY, not a structural ceiling").

The N-level structural capability of ``lineage`` is preserved —
nothing here hard-codes "2" or assumes fixed depth.

All times stored as ``REAL`` unix-seconds (float).
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from .state_db_acl_policy import (
    DEFAULT_COMMS_POLICY,
    apply_may_spawn_gate,
    read_comms_policy,
    record_comms_policy,
    sender_target_relationship,
)

__all__ = [
    "CommsNodeConflictError",
    "DEFAULT_COMMS_POLICY",
    "apply_may_spawn_gate",
    "derive_group",
    "grant_send",
    "has_grant",
    "is_developer",
    "is_local_node",
    "list_comms_grants",
    "list_comms_nodes",
    "list_node_tokens",
    "lookup_comms_node",
    "mint_node_token",
    "named_groups_peered",
    "read_comms_policy",
    "record_comms_policy",
    "record_lineage",
    "register_comms_node",
    "resolve_group_name",
    "resolve_node_host",
    "resolve_node_token",
    "revoke_send",
    "same_named_group",
    "sender_target_relationship",
    "spawn_allowed",
    "unregister_comms_node",
]


# ---------------------------------------------------------------------------
# node_tokens — authenticated identity (handoff §4 acceptance: "identity
# cannot be spoofed via a metadata field")
# ---------------------------------------------------------------------------

# 256 bits of entropy. URL-safe base64 → ~43 chars.
_TOKEN_BYTES = 32


def mint_node_token(*, name: str, db_path: Path | None = None) -> str:
    """Return the bearer token for ``name``, minting one if absent.

    Idempotent: re-registration returns the existing token rather
    than rotating, so an active agent's ``Authorization: Bearer ...``
    header keeps working across a re-register. Rotation, when needed,
    is a separate operation (not implemented here).

    Raises ``ValueError`` if ``name`` is empty.
    """
    if not name:
        raise ValueError("mint_node_token: name must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT token FROM node_tokens WHERE name = ?", (name,)
        ).fetchone()
        if existing is not None:
            return str(existing["token"])
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = time.time()
        conn.execute(
            "INSERT INTO node_tokens (name, token, created_at) VALUES (?, ?, ?)",
            (name, token, now),
        )
    return token


def resolve_node_token(
    *,
    token: str,
    db_path: Path | None = None,
) -> str | None:
    """Map a bearer token back to a node name; ``None`` if unknown.

    Returns ``None`` for an empty token (defence-in-depth — the
    middleware already rejects requests with no Authorization
    header, but we never resolve ``""`` to a real identity).
    """
    if not token:
        return None
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM node_tokens WHERE token = ?", (token,)
        ).fetchone()
    if row is None:
        return None
    return str(row["name"])


def list_node_tokens(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return ``[{name, created_at}, ...]`` over every minted token.

    Observability surface for the host operator. The token value
    itself is deliberately NOT returned — that would defeat the
    purpose of storing it as a secret.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute("SELECT name, created_at FROM node_tokens ORDER BY name ASC")
        return [
            {"name": str(r["name"]), "created_at": float(r["created_at"])}
            for r in cur.fetchall()
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

    Phase-3 (ADR-0010 Step 2): if ``name``'s ``node_comms_policy`` row
    sets ``lineage_group = 'solitary'``, the group is forced to
    ``{name}`` and the lineage-table walk is skipped. That isolates a
    capsule from its siblings AND its parent without depending on the
    lineage table being empty — clew capsule children adopt this so a
    sibling capsule can never address them through the group-default
    ACL even though they share a parent edge.
    """
    if not name:
        raise ValueError("derive_group: name must be non-empty")
    # Phase-3 solitary override — short-circuits to the singleton group
    # without touching the lineage table.
    policy = read_comms_policy(name=name, db_path=db_path)
    if policy["lineage_group"] == "solitary":
        return {name}
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
# named groups (operator 2026-06-25) — a SECOND grouping axis layered on
# top of the lineage-derived group mesh above. The readers live in a
# sibling module (state_db_named_groups) under the per-file line cap; they
# are re-exported below so the natural import path
# ``from ..._state.state_db_nodes import same_named_group`` keeps working.
# The group NAME is resolved at agent_start from ``metadata.labels.group``
# (else role-derived) and persisted in ``node_comms_policy.group_name``;
# the resolver / peering allowlist live in
# :mod:`scitex_agent_container.config._group_resolver`.
# ---------------------------------------------------------------------------

from .state_db_named_groups import (  # noqa: E402
    is_developer,
    named_groups_peered,
    resolve_group_name,
    same_named_group,
)

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
        # Admin / human operator. Skips the global root-only check;
        # per-spec may_spawn (Phase-3 Gap-5) layers on top.
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
    from .state_db import open_db

    with open_db(db_path) as conn:
        parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (caller,)
        ).fetchone()
    if parent_row is None:
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
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


def grant_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
    note: str | None = None,
) -> None:
    """Insert (or refresh) a cross-group grant ``sender → target``.

    Idempotent — re-granting the same pair leaves the row untouched
    (timestamp not bumped). The ``sender`` identity is authenticated
    by :class:`_listen._acl.NodeAuthMiddleware` resolving the bearer;
    the optional ``note`` is a free-form audit annotation (e.g. the
    ticket / handoff that authorised the grant).
    """
    if not sender or not target:
        raise ValueError("grant_send: sender and target must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM comms_grants WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comms_grants "
            "(sender_name, target_name, created_at, note) "
            "VALUES (?, ?, ?, ?)",
            (sender, target, time.time(), note),
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
            "DELETE FROM comms_grants WHERE sender_name = ? AND target_name = ?",
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
            "SELECT 1 FROM comms_grants WHERE sender_name = ? AND target_name = ?",
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
# WI-4 / ADR-0014 — name → host resolver primitives. Extracted into a
# sibling module (state_db_host_resolver) under the per-file line cap and
# re-exported below so the natural import path
# ``from ..._state.state_db_nodes import resolve_node_host`` keeps working;
# the cross-host forwarder consults both the resolver and the ACL
# primitives from this module.
# ---------------------------------------------------------------------------

from .state_db_host_resolver import (  # noqa: E402
    is_local_node,
    resolve_node_host,
)


# ---------------------------------------------------------------------------
# ADR-0014 — comms_nodes federated graph. Primitives live in a sibling
# module (state_db_comms_nodes) under the per-file line cap; re-exported
# here so the natural import path
# ``from ..._state.state_db_nodes import register_comms_node`` works.
# ---------------------------------------------------------------------------

from .state_db_comms_nodes import (  # noqa: E402, F401
    CommsNodeConflictError,
    list_comms_nodes,
    lookup_comms_node,
    register_comms_node,
    unregister_comms_node,
)
