"""Phase-3 capsule-isolation policy persistence (ADR-0010 Step 2).

Lives in a sibling module so :mod:`.state_db_nodes` stays under the
per-file line cap. The functions below are re-exported from
``state_db_nodes`` so the existing import surface stays:

    from scitex_agent_container._state.state_db_nodes import (
        record_comms_policy, read_comms_policy, sender_target_relationship,
    )

Schema (see ``_SCHEMA_REGISTRY`` in :mod:`.state_db`):

    CREATE TABLE node_comms_policy (
        name              TEXT PRIMARY KEY,
        outbound_siblings TEXT NOT NULL DEFAULT 'allow',
        outbound_parent   TEXT NOT NULL DEFAULT 'allow',
        inbound_siblings  TEXT NOT NULL DEFAULT 'allow',
        inbound_parent    TEXT NOT NULL DEFAULT 'allow',
        lineage_group     TEXT NOT NULL DEFAULT '',
        may_spawn         INTEGER NOT NULL DEFAULT 1,
        updated_at        REAL NOT NULL
    );

Row written at ``agent_start`` from the loaded ``spec.comms`` /
``spec.lineage`` blocks; read at ACL-check time by
:func:`scitex_agent_container._listen._acl.check_send_acl` and
:func:`scitex_agent_container._listen._acl.check_spawn`. Defaults match
the dataclass defaults so a missing row is byte-equivalent to the
pre-Phase-3 group-default ACL.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_COMMS_POLICY",
    "apply_may_spawn_gate",
    "read_comms_policy",
    "record_comms_policy",
    "sender_target_relationship",
]


def apply_may_spawn_gate(
    *,
    caller: str | None,
    base: tuple[bool, str | None],
    db_path: Path | None,
) -> tuple[bool, str | None]:
    """Layer ``spec.lineage.may_spawn=false`` on top of the global policy.

    Evaluated AFTER the global policy result: a deny stays a deny
    (existing reason preserved). If the global path allowed but the
    caller has ``may_spawn=False`` in its persisted policy, the allow
    flips to a per-spec deny with a clear reason. Admin path
    (``caller=None``) has no name to look up — kept untouched, so
    operator-launched starts never trip this.
    """
    if not base[0]:
        return base
    if not caller:
        return base
    policy = read_comms_policy(name=caller, db_path=db_path)
    if not policy["may_spawn"]:
        return (
            False,
            (
                f"spawn denied: caller {caller!r} has "
                "spec.lineage.may_spawn=false in its agent definition; "
                "the per-spec deny survives global-policy relaxation."
            ),
        )
    return base


DEFAULT_COMMS_POLICY: dict[str, Any] = {
    "outbound_siblings": "allow",
    "outbound_parent": "allow",
    "inbound_siblings": "allow",
    "inbound_parent": "allow",
    "lineage_group": "",
    "may_spawn": True,
}


def record_comms_policy(
    *,
    name: str,
    outbound_siblings: str = "allow",
    outbound_parent: str = "allow",
    inbound_siblings: str = "allow",
    inbound_parent: str = "allow",
    lineage_group: str = "",
    may_spawn: bool = True,
    db_path: Path | None = None,
) -> None:
    """Upsert the Phase-3 per-spec ACL policy for ``name``.

    Called from core ``agent_start`` after the spawn-gate runs, so the
    row always reflects the *current* ``spec.comms`` / ``spec.lineage``
    blocks on disk. A re-start refreshes the row in place (a spec edit
    becomes live on the next start without manual state.db surgery).

    Raises :class:`ValueError` on an empty name or out-of-domain values
    (the parser/validator already reject these — defence-in-depth).
    """
    if not name:
        raise ValueError("record_comms_policy: name must be non-empty")
    if outbound_siblings not in ("allow", "deny"):
        raise ValueError(
            f"outbound_siblings must be 'allow' or 'deny', got "
            f"{outbound_siblings!r}"
        )
    if outbound_parent not in ("allow", "deny"):
        raise ValueError(
            f"outbound_parent must be 'allow' or 'deny', got "
            f"{outbound_parent!r}"
        )
    if inbound_siblings not in ("allow", "deny"):
        raise ValueError(
            f"inbound_siblings must be 'allow' or 'deny', got "
            f"{inbound_siblings!r}"
        )
    if inbound_parent not in ("allow", "deny"):
        raise ValueError(
            f"inbound_parent must be 'allow' or 'deny', got "
            f"{inbound_parent!r}"
        )
    if lineage_group not in ("", "solitary"):
        raise ValueError(
            f"lineage_group must be '' or 'solitary', got {lineage_group!r}"
        )
    if not isinstance(may_spawn, bool):
        raise ValueError(
            f"may_spawn must be a bool, got {type(may_spawn).__name__}"
        )
    from .state_db import open_db

    now = time.time()
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO node_comms_policy ("
            "name, outbound_siblings, outbound_parent, "
            "inbound_siblings, inbound_parent, lineage_group, "
            "may_spawn, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "outbound_siblings=excluded.outbound_siblings, "
            "outbound_parent=excluded.outbound_parent, "
            "inbound_siblings=excluded.inbound_siblings, "
            "inbound_parent=excluded.inbound_parent, "
            "lineage_group=excluded.lineage_group, "
            "may_spawn=excluded.may_spawn, "
            "updated_at=excluded.updated_at",
            (
                name,
                outbound_siblings,
                outbound_parent,
                inbound_siblings,
                inbound_parent,
                lineage_group,
                1 if may_spawn else 0,
                now,
            ),
        )


def read_comms_policy(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Return the per-spec ACL policy for ``name``, or defaults if absent.

    A missing row yields :data:`DEFAULT_COMMS_POLICY` so the
    "no-row" vs "row-with-default-values" distinction is invisible to
    callers. Defaults are byte-equivalent to pre-Phase-3 behaviour.
    """
    if not name:
        return dict(DEFAULT_COMMS_POLICY)
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT outbound_siblings, outbound_parent, "
            "inbound_siblings, inbound_parent, lineage_group, may_spawn "
            "FROM node_comms_policy WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        return dict(DEFAULT_COMMS_POLICY)
    return {
        "outbound_siblings": str(row["outbound_siblings"]),
        "outbound_parent": str(row["outbound_parent"]),
        "inbound_siblings": str(row["inbound_siblings"]),
        "inbound_parent": str(row["inbound_parent"]),
        "lineage_group": str(row["lineage_group"]),
        "may_spawn": bool(row["may_spawn"]),
    }


def sender_target_relationship(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> str:
    """Classify the ``sender → target`` lineage relationship.

    Returns one of:

    * ``"self"``    — same node (trivial self-send).
    * ``"parent"``  — target is sender's parent in the lineage table.
    * ``"child"``   — target is one of sender's direct children.
    * ``"sibling"`` — sender and target share the same parent.
    * ``"other"``   — no lineage path (cross-group or unrelated).

    Used by :func:`scitex_agent_container._listen._acl.check_send_acl`
    to apply the per-spec outbound/inbound policy on the right edge.
    Pure read of the ``lineage`` table — no policy state consulted.
    """
    if not sender or not target:
        return "other"
    if sender == target:
        return "self"
    from .state_db import open_db

    with open_db(db_path) as conn:
        sender_parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (sender,)
        ).fetchone()
        target_parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (target,)
        ).fetchone()
    sender_parent = (
        str(sender_parent_row["parent_name"]) if sender_parent_row else None
    )
    target_parent = (
        str(target_parent_row["parent_name"]) if target_parent_row else None
    )
    if sender_parent == target:
        return "parent"
    if target_parent == sender:
        return "child"
    if (
        sender_parent is not None
        and target_parent is not None
        and sender_parent == target_parent
    ):
        return "sibling"
    return "other"
