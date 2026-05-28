"""``comms_nodes`` primitives — symmetric federated comms graph (ADR-0014).

Why a separate module: ``state_db_nodes`` already owns the WI-2 ACL
primitives (lineage / grants / tokens) plus the original
``resolve_node_host`` against ``instances``. Adding the comms_nodes
CRUD there would push the file over the per-file line cap; siblings
``state_db_diary``, ``state_db_heartbeats`` etc. set the precedent of
splitting helpers along table boundaries.

Public symbols are re-exported from :mod:`state_db_nodes` so callers
can use the natural import path
``from ..._state.state_db_nodes import register_comms_node``.

ADR-0014 context: the cross-host A2A bug is "spartan-agent → lead
fails because Spartan has no ``instances`` row for ``lead``". The fix
is a symmetric federated table that every host writes locally (operator
identity at listen start, agent-start hook for spawned agents) and that
``sac registry sync`` ssh-pulls from every peer. The ``resolve_node_host``
extension (in :mod:`state_db_nodes`) consults this table when no live
``instances`` row matches.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

__all__ = [
    "CommsNodeConflictError",
    "list_comms_nodes",
    "lookup_comms_node",
    "register_comms_node",
    "resolve_comms_node_host",
    "unregister_comms_node",
]


class CommsNodeConflictError(RuntimeError):
    """Two hosts claim the same ``name`` with different ``(host, a2a_port)``.

    Raised by :func:`register_comms_node` when the existing row was
    sync'd from a different ``source_host`` than the caller and the
    new (host, a2a_port) pair disagrees with what's already stored.

    ADR-0014 conflict policy: fail-loud (α) over last-writer-wins (β).
    Silent LWW would let a misconfigured peer stomp the authoritative
    row on the next pull; making the operator resolve the collision
    is the safe default.

    The exception carries enough context (name, existing host/port +
    source, new host/port + source) for the caller's log line to point
    at the misconfig directly.
    """


def register_comms_node(
    *,
    name: str,
    host: str,
    a2a_port: int,
    source_host: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Idempotent upsert into ``comms_nodes``.

    Behaviour:

    * No existing row → INSERT a new one. ``registered_at`` and
      ``updated_at`` are set to ``time.time()``.
    * Existing row with matching ``(host, a2a_port)`` → bump
      ``updated_at`` only. ``ended_at`` is cleared if set (re-activates
      a tombstoned row, which is the natural way a "node came back"
      sync converges).
    * Existing row with DIFFERENT ``(host, a2a_port)`` AND a different
      ``source_host`` → raise :class:`CommsNodeConflictError`. Same
      ``source_host`` overwriting is allowed (the originating host is
      the authoritative reporter for its own rows).

    The ``source_host`` distinguishes "I'm hearing about this node
    from peer X" (sync) from "this is a local registration" (NULL).
    Two hosts independently claiming the same name is the collision
    case fail-loud is designed to catch.
    """
    if not name:
        raise ValueError("register_comms_node: name must be non-empty")
    if not host:
        raise ValueError("register_comms_node: host must be non-empty")
    if not isinstance(a2a_port, int) or isinstance(a2a_port, bool) or a2a_port <= 0:
        raise ValueError(
            f"register_comms_node: a2a_port must be a positive int, got {a2a_port!r}"
        )
    from .state_db import open_db

    now = time.time()
    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT host, a2a_port, source_host, ended_at "
            "FROM comms_nodes WHERE name = ?",
            (name,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO comms_nodes "
                "(name, host, a2a_port, registered_at, updated_at, "
                " source_host, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (name, host, a2a_port, now, now, source_host),
            )
            return
        same_target = (
            str(existing["host"]) == host
            and int(existing["a2a_port"]) == a2a_port
        )
        existing_source = existing["source_host"]
        if same_target:
            # Idempotent — bump updated_at and clear any tombstone.
            conn.execute(
                "UPDATE comms_nodes SET updated_at = ?, ended_at = NULL, "
                "source_host = ? WHERE name = ?",
                (now, source_host, name),
            )
            return
        # Different target — only allow when the SAME source is updating
        # its own row (e.g. an operator re-bound the listen on a new port).
        if existing_source == source_host:
            conn.execute(
                "UPDATE comms_nodes SET host = ?, a2a_port = ?, "
                "updated_at = ?, ended_at = NULL WHERE name = ?",
                (host, a2a_port, now, name),
            )
            return
        raise CommsNodeConflictError(
            f"comms_nodes name conflict for {name!r}: "
            f"existing=(host={existing['host']!r}, port={int(existing['a2a_port'])}, "
            f"source={existing_source!r}) "
            f"new=(host={host!r}, port={a2a_port}, source={source_host!r}). "
            f"ADR-0014: names are globally unique. Rename or unregister one "
            f"and re-run `sac registry sync --all`."
        )


def unregister_comms_node(
    *,
    name: str,
    db_path: Path | None = None,
) -> bool:
    """Tombstone the row by setting ``ended_at = time.time()``.

    Returns ``True`` iff a live (un-tombstoned) row was tombstoned.
    Re-running on an already-tombstoned row is a no-op returning
    ``False``. The row is preserved (not deleted) so the next
    :func:`export_state` carries the deletion to peers via
    ``import_state``'s ``INSERT OR IGNORE`` — which, for an existing
    PK, will need an UPDATE-shaped sync (future work).

    For Stage 1 (ADR-0014): tombstone is read by
    :func:`lookup_comms_node` / :func:`resolve_comms_node_host` which
    filter ``ended_at IS NULL`` so a tombstoned row is invisible to
    the resolver. GC of old tombstones is a separate maintenance
    pass (out of scope for Stage 1).
    """
    if not name:
        return False
    from .state_db import open_db

    now = time.time()
    with open_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE comms_nodes SET ended_at = ?, updated_at = ? "
            "WHERE name = ? AND ended_at IS NULL",
            (now, now, name),
        )
        return cur.rowcount > 0


def lookup_comms_node(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return the live ``comms_nodes`` row for ``name`` as a dict, or None.

    Tombstoned rows (``ended_at`` set) are filtered out — for the
    resolver they are equivalent to "not present". Callers that need
    to *see* tombstones for sync purposes use :func:`list_comms_nodes`
    with ``include_ended=True``.
    """
    if not name:
        return None
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name, host, a2a_port, registered_at, updated_at, "
            "       source_host, ended_at "
            "FROM comms_nodes WHERE name = ? AND ended_at IS NULL",
            (name,),
        ).fetchone()
    if row is None:
        return None
    return {
        "name": str(row["name"]),
        "host": str(row["host"]),
        "a2a_port": int(row["a2a_port"]),
        "registered_at": float(row["registered_at"]),
        "updated_at": float(row["updated_at"]),
        "source_host": (
            str(row["source_host"]) if row["source_host"] is not None else None
        ),
        "ended_at": (
            float(row["ended_at"]) if row["ended_at"] is not None else None
        ),
    }


def resolve_comms_node_host(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Resolver-shaped lookup for cross-host A2A forwarding.

    Returns ``{host, a2a_port}`` (matching the
    :func:`state_db_nodes.resolve_node_host` shape) or ``None`` when
    the name is missing OR tombstoned. Used by ``resolve_node_host``
    as the fallback after the ``instances`` lookup misses.
    """
    info = lookup_comms_node(name=name, db_path=db_path)
    if info is None:
        return None
    return {"host": info["host"], "a2a_port": info["a2a_port"]}


def list_comms_nodes(
    *,
    db_path: Path | None = None,
    include_ended: bool = False,
) -> list[dict[str, Any]]:
    """Return every ``comms_nodes`` row as a list of dicts.

    Default filters out tombstoned rows; pass ``include_ended=True``
    for the full table (e.g. an operator inspecting what's about to
    be GC'd, or a sync debug). Order is by ``name`` ascending for
    deterministic output.
    """
    from .state_db import open_db

    where = "" if include_ended else "WHERE ended_at IS NULL"
    sql = (
        "SELECT name, host, a2a_port, registered_at, updated_at, "
        "       source_host, ended_at "
        f"FROM comms_nodes {where} ORDER BY name ASC".strip()
    )
    with open_db(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {
            "name": str(r["name"]),
            "host": str(r["host"]),
            "a2a_port": int(r["a2a_port"]),
            "registered_at": float(r["registered_at"]),
            "updated_at": float(r["updated_at"]),
            "source_host": (
                str(r["source_host"]) if r["source_host"] is not None else None
            ),
            "ended_at": (
                float(r["ended_at"]) if r["ended_at"] is not None else None
            ),
        }
        for r in rows
    ]
