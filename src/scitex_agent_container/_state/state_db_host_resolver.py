"""WI-4 / ADR-0014 — name → host resolver primitives.

Extracted from :mod:`.state_db_nodes` (which the merged group-ACL
foundation pushed over the per-file line cap) into this sibling module,
mirroring the existing ``state_db_acl_policy`` / ``state_db_comms_nodes``
/ ``state_db_named_groups`` split. The two functions are re-exported from
``state_db_nodes`` so the natural import path
``from ..._state.state_db_nodes import resolve_node_host`` keeps working —
the cross-host forwarder consults both the resolver and the ACL
primitives from that module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "is_local_node",
    "resolve_node_host",
]


def resolve_node_host(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Map a node ``name`` to ``{host, a2a_port}``.

    Resolution order (ADR-0014):

    1. ``instances`` table — the canonical "live agent" registry. Picks
       the most recently started live (``ended_at IS NULL``) row.
    2. ``comms_nodes`` table (ADR-0014 federated comms graph) — used
       for nodes that are NOT sac-managed agents (operator identities
       like ``lead``, peer hosts' listen-targets, cross-host
       registrations sync'd via ``sac registry sync``).

    Returns ``None`` only when neither table knows the name. Callers
    treat ``None`` as "this is a local-only/unknown node; do not
    cross-host forward" — the ``NodeRegistry`` implicit-registration
    path in ``_listen/_node_channel.py`` handles that case.
    """
    if not name:
        return None
    from .state_db import open_db
    from .state_db_comms_nodes import resolve_comms_node_host

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
    if row is not None:
        return {
            "host": str(row["host"]),
            "a2a_port": int(row["a2a_port"]) if row["a2a_port"] is not None else None,
        }
    # Fall through to comms_nodes (ADR-0014).
    return resolve_comms_node_host(name=name, db_path=db_path)


def is_local_node(
    *,
    name: str,
    local_host: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` if ``name`` should be served locally.

    Local cases:

    * The name resolves to ``local_host`` via :func:`resolve_node_host`
      (either ``instances`` or ``comms_nodes`` per ADR-0014).
    * The name does NOT resolve to any host (unknown / external node) —
      defer to the local ``NodeRegistry`` implicit-registration path.
      Forwarding a never-seen name would synthesise an SSRF target
      from a self-claimed string; the host-local path is correct.

    Critically: when the name IS in ``comms_nodes`` with a host that
    is NOT ``local_host``, this returns ``False`` — that is the bug
    fix the federated graph closes (cross-host targets like ``lead``
    on a Spartan host are now correctly forwarded instead of being
    treated as local).
    """
    info = resolve_node_host(name=name, db_path=db_path)
    if info is None:
        return True
    return info["host"] == local_host
