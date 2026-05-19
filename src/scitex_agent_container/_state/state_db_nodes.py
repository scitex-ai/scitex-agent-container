"""Per-node tokens + lineage primitives for WI-2 ACL.

The handoff (``HANDOFF_AGENT_COMMS_2026-05-19.md`` §4 / WI-2) requires:

  > Authenticated sender identity. The ACL check is only as strong
  > as the identity — the sender identity carried in
  > ``params.metadata`` must be authenticated (per-node credential /
  > bearer token), never a self-claimed name. **Do not gate on an
  > unauthenticated string.**

and:

  > Group-based default ACL. ... The group is derived from lineage —
  > no per-pair config for the common case.

This module supplies the two state.db primitives that satisfy both
requirements:

* :func:`mint_node_token` / :func:`resolve_node_token` —
  authenticated identity. Each node (sac-managed or external) gets a
  bearer token at registration; the listen server resolves an
  incoming ``Authorization: Bearer <token>`` to a node name via
  ``resolve_node_token``.

* :func:`record_lineage` / :func:`derive_group` — lineage and group
  derivation. ``record_lineage(child, parent)`` is called by
  ``sac agents start``; ``derive_group(name)`` returns the set of
  nodes inside the same default-ACL bubble (parent + direct
  children).

Both pieces stay N-level capable per handoff §2 D5 ("Depth limit is
a POLICY, not a structural ceiling"). The two-level cap currently in
force is a separate policy gate, NOT enforced by these primitives.

All times stored as ``REAL`` unix-seconds (float). Matches the diary
tables.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

__all__ = [
    "derive_group",
    "list_node_tokens",
    "mint_node_token",
    "record_lineage",
    "resolve_node_token",
]


# 256 bits of entropy. URL-safe base64 → ~43 chars; comfortably above
# the 32-char floor the tests assert against.
_TOKEN_BYTES = 32


def mint_node_token(*, name: str, db_path: Path | None = None) -> str:
    """Return the bearer token for ``name``, minting one if absent.

    Idempotent: re-registration returns the existing token rather
    than rotating, so an active agent's ``Authorization: Bearer ...``
    header keeps working across a re-register. Rotation, when needed,
    is a separate operation (not implemented here).

    Raises if ``name`` is empty.
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


def resolve_node_token(*, token: str, db_path: Path | None = None) -> str | None:
    """Map a bearer token back to a node name; ``None`` if unknown.

    Returns ``None`` for an empty token rather than treating it as a
    valid lookup — the listen server's middleware already rejects
    requests with no Authorization header, but defence-in-depth means
    we never resolve ``""`` to a real identity.
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

    The derivation is intentionally local — it never walks the
    full lineage tree. That keeps the default-ACL semantics simple
    and matches handoff §2: "the group is the unit of default ACL"
    (one parent + its direct children, not the entire ancestry).
    """
    if not name:
        raise ValueError("derive_group: name must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        # Is ``name`` a parent?
        children_rows = conn.execute(
            "SELECT child_name FROM lineage WHERE parent_name = ?", (name,)
        ).fetchall()
        if children_rows:
            group: set[str] = {name}
            for r in children_rows:
                group.add(str(r["child_name"]))
            return group
        # Is ``name`` a child?
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


def list_node_tokens(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return ``[{name, created_at}, ...]`` over every minted token.

    Observability surface for the host operator (``sac db query
    --table=node_tokens`` already exists via the generic table-walker;
    this helper is a typed-API alias). The token value itself is
    deliberately NOT returned — that would defeat the purpose of
    storing it as a secret.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT name, created_at FROM node_tokens ORDER BY name ASC"
        )
        return [{"name": str(r["name"]), "created_at": float(r["created_at"])}
                for r in cur.fetchall()]
