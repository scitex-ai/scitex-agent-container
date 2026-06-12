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
from typing import Any, Literal

__all__ = [
    "CommsNodeConflictError",
    "RegisterCommsNodeKind",
    "list_comms_nodes",
    "lookup_comms_node",
    "register_comms_node",
    "resolve_comms_node_host",
    "unregister_comms_node",
]


RegisterCommsNodeKind = Literal["spec", "self-peer", "manual"]
"""Discriminator passed by callers of :func:`register_comms_node`.

``spec`` — the row is being written from the canonical container-spec
path (``_lifecycle/_instances._record_local_instance`` after a spec-
driven ``sac start``).

``self-peer`` — the row is being written from a self-peer registration
path (``_mcp/_channel_self_register.register_self_node`` or the Q4
``_listen/_self_peer_persistence.persist_discovered_self_peers``).

``manual`` — operator-driven ``sac registry register`` or a test
fixture. Default when the caller doesn't pass one explicitly. The
kind is NOT persisted into ``comms_nodes`` (no schema change) — it
flows into :class:`CommsNodeConflictError`'s message so the operator
sees WHICH path tried to overwrite WHICH.
"""


class CommsNodeConflictError(RuntimeError):
    """Two registrations disagree on a ``name``'s ``(host, a2a_port)``.

    Raised by :func:`register_comms_node` whenever a write would
    silently OVERWRITE an existing row with a different
    ``(host, a2a_port)``. Two collision shapes share this exception
    (operator directive 12847 — fail-loud, no silent winner):

    1. **Cross-host conflict.** Existing row was sync'd from
       ``source_host=A``; the caller registers with
       ``source_host=B`` and a different ``(host, a2a_port)``.
       Two hosts independently claim the same name — neither has
       authority over the other.
    2. **Same-source different-target conflict (PR L1).** Existing
       row and caller both have the SAME ``source_host`` (e.g. both
       are local registrations with ``source_host=None``) but the
       caller's ``(host, a2a_port)`` differs from what's stored.
       Until PR L1 this silently last-writer-wins; that is the exact
       silent-shadow the operator's directive locks out. The caller
       must pass ``replace=True`` to opt into the overwrite (only
       reached deliberately through the upcoming ``--prefer`` flag,
       which is the explicit-client-option half of the directive).

    ADR-0014 conflict policy: fail-loud (α) over last-writer-wins (β).
    The exception carries enough context (kind, source_path, existing
    host/port + source, new host/port + source) for the caller's log
    line to point at the misconfig directly.
    """


def register_comms_node(
    *,
    name: str,
    host: str,
    a2a_port: int,
    source_host: str | None = None,
    db_path: Path | None = None,
    kind: RegisterCommsNodeKind = "manual",
    source_path: str | None = None,
    replace: bool = False,
) -> None:
    """Idempotent upsert into ``comms_nodes``.

    Behaviour (PR L1, operator directive 12847 — fail-loud, no silent
    last-writer-wins overwrite):

    * No existing row → INSERT a new one. ``registered_at`` and
      ``updated_at`` are set to ``time.time()``.
    * Existing row with matching ``(host, a2a_port)`` → bump
      ``updated_at`` only. ``ended_at`` is cleared if set (re-activates
      a tombstoned row, which is the natural way a "node came back"
      sync converges).
    * Existing row with DIFFERENT ``(host, a2a_port)`` AND a different
      ``source_host`` → raise :class:`CommsNodeConflictError`. Two
      hosts independently claim the same name; neither has authority
      over the other (operator-rename is the only resolution).
    * Existing row with DIFFERENT ``(host, a2a_port)`` but the SAME
      ``source_host`` — until PR L1 this silently overwrote the row.
      It now raises :class:`CommsNodeConflictError` UNLESS the caller
      opts in with ``replace=True``. The opt-in is wired by the
      upcoming ``--prefer spec|self`` operator flag (the explicit-
      client-option half of the directive). Default callers — the
      spec-driven paired-write, the channel self-register, the Q4
      self-peer persistence — do NOT set ``replace=True``; they catch
      the exception and log, so a real collision surfaces in the
      operator's logs and no row is silently shadowed.

    Parameters
    ----------
    kind:
        Discriminator for the calling path (``spec`` / ``self-peer``
        / ``manual``). Flows into the error message so the operator
        sees WHICH path tried to overwrite WHICH; NOT persisted (no
        schema change). Default ``"manual"`` keeps the existing
        public surface backwards-compatible for callers that don't
        pass it (operator-driven ``sac registry register``, tests).
    source_path:
        Optional caller-supplied source identifier (the spec file
        path for ``kind="spec"``, the discovered
        ``agents/<n>/spec.yaml`` path for ``kind="self-peer"``, a
        CLI invocation tag for ``kind="manual"``). Surfaces in the
        error message so the operator can disambiguate by path
        (per operator's "distinguishable by path anyway" hint).
    replace:
        Opt-in to overwrite an existing same-source row with a
        different ``(host, a2a_port)``. Wired by the ``--prefer``
        flag once that PR lands; passing it from elsewhere is the
        explicit-replace contract and bypasses the loud-fail check.
        Has no effect on the cross-host (different ``source_host``)
        conflict — that one ALWAYS raises.
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
            str(existing["host"]) == host and int(existing["a2a_port"]) == a2a_port
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
        # Different target.
        if existing_source != source_host:
            # Cross-host conflict — always raise; operator rename is the
            # only resolvable path.
            raise CommsNodeConflictError(
                f"comms_nodes name conflict for {name!r}: "
                f"existing=(host={existing['host']!r}, "
                f"port={int(existing['a2a_port'])}, "
                f"source={existing_source!r}) "
                f"new=(kind={kind!r}, host={host!r}, port={a2a_port}, "
                f"source={source_host!r}, source_path={source_path!r}). "
                f"ADR-0014: names are globally unique. Rename or "
                f"unregister one and re-run `sac registry sync --all`."
            )
        # Same source, different target. PR L1 (operator directive
        # 12847) — fail loud on collision, no silent last-writer-wins.
        if not replace:
            other_kind = "spec" if kind == "self-peer" else "self-peer pointer"
            raise CommsNodeConflictError(
                f"comms_nodes silent-overwrite refused for {name!r} "
                f"(operator directive 12847, PR L1): "
                f"existing=(host={existing['host']!r}, "
                f"port={int(existing['a2a_port'])}, "
                f"source={existing_source!r}) "
                f"incoming=(kind={kind!r}, host={host!r}, "
                f"port={a2a_port}, source={source_host!r}, "
                f"source_path={source_path!r}). "
                f"Two registrations for the same name disagree on the "
                f"(host, a2a_port) target. Resolve by either:\n"
                f"  - rerunning the canonical writer with "
                f"`--prefer {kind}` to declare intent (overwrites), or\n"
                f"  - removing/renaming the conflicting {other_kind} "
                f"so a single source owns this name."
            )
        # Explicit replace — operator-confirmed via --prefer flag.
        conn.execute(
            "UPDATE comms_nodes SET host = ?, a2a_port = ?, "
            "updated_at = ?, ended_at = NULL WHERE name = ?",
            (host, a2a_port, now, name),
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
        "ended_at": (float(row["ended_at"]) if row["ended_at"] is not None else None),
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
            "ended_at": (float(r["ended_at"]) if r["ended_at"] is not None else None),
        }
        for r in rows
    ]
