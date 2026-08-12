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
        group_name        TEXT NOT NULL DEFAULT '',
        group_names       TEXT NOT NULL DEFAULT '',
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
    # Group-based ACL (operator 2026-06-25): the agent's PRIMARY named
    # group — the single bucket the default-ACL mesh resolves through.
    # "" (ungrouped) is the default; absence is byte-equivalent to the
    # pre-group-name behaviour (same-group allow needs a non-empty match).
    "group_name": "",
    # EVERY named group the spec's ``metadata.labels`` lists (incident
    # 2026-08-10). The AUTHORITY gates read this set, not the primary
    # above, so an agent authored as ``groups: [generalist, developer]``
    # is a developer regardless of list order. Empty tuple on a row
    # written before the column existed — ``resolve_group_names`` unions
    # it with ``group_name``, so that row keeps its old meaning exactly.
    "group_names": (),
}


def _split_group_names(raw: str) -> tuple[str, ...]:
    """Decode the comma-separated ``group_names`` column into a tuple.

    Blank / whitespace-only members are dropped, so ``""`` decodes to the
    empty tuple and a trailing comma is harmless.
    """
    return tuple(part.strip() for part in str(raw or "").split(",") if part.strip())


def _join_group_names(groups) -> str:
    """Encode an iterable of group names for the ``group_names`` column.

    De-duplicated and SORTED, so the stored string is deterministic for a
    given set (the column answers a MEMBERSHIP question — order carries
    no meaning, and a stable encoding keeps diffs and denial messages
    readable). Blank members are dropped.

    Raises :class:`ValueError` on a name containing a comma: the encoding
    is comma-separated, so accepting one would silently split a single
    group into two. Fail loudly rather than corrupt an ACL input.
    """
    if groups is None:
        return ""
    if isinstance(groups, str):
        raise ValueError(
            "group_names must be an iterable of group names, not a bare "
            f"string ({groups!r}) — pass e.g. ['developer']"
        )
    out: set[str] = set()
    for item in groups:
        if item is None:
            continue
        trimmed = str(item).strip()
        if not trimmed:
            continue
        if "," in trimmed:
            raise ValueError(
                f"group name {trimmed!r} contains a comma; the group_names "
                "column is comma-separated and cannot encode it"
            )
        out.add(trimmed)
    return ",".join(sorted(out))


def record_comms_policy(
    *,
    name: str,
    outbound_siblings: str = "allow",
    outbound_parent: str = "allow",
    inbound_siblings: str = "allow",
    inbound_parent: str = "allow",
    lineage_group: str = "",
    may_spawn: bool = True,
    group_name: str = "",
    group_names=None,
    db_path: Path | None = None,
) -> None:
    """Upsert the Phase-3 per-spec ACL policy for ``name``.

    Called from core ``agent_start`` after the spawn-gate runs, so the
    row always reflects the *current* ``spec.comms`` / ``spec.lineage``
    blocks on disk. A re-start refreshes the row in place (a spec edit
    becomes live on the next start without manual state.db surgery).

    ``group_name`` is the PRIMARY group (the default-ACL mesh bucket);
    ``group_names`` is EVERY group the spec names (the authority set).
    Both are projections of the same ``metadata.labels`` and are written
    together — that is what keeps them from disagreeing. ``group_names``
    defaults to ``None``, which stores the PRIMARY alone, so an existing
    caller that passes only ``group_name`` keeps its exact old meaning.

    Raises :class:`ValueError` on an empty name or out-of-domain values
    (the parser/validator already reject these — defence-in-depth).
    """
    if not name:
        raise ValueError("record_comms_policy: name must be non-empty")
    if outbound_siblings not in ("allow", "deny"):
        raise ValueError(
            f"outbound_siblings must be 'allow' or 'deny', got {outbound_siblings!r}"
        )
    if outbound_parent not in ("allow", "deny"):
        raise ValueError(
            f"outbound_parent must be 'allow' or 'deny', got {outbound_parent!r}"
        )
    if inbound_siblings not in ("allow", "deny"):
        raise ValueError(
            f"inbound_siblings must be 'allow' or 'deny', got {inbound_siblings!r}"
        )
    if inbound_parent not in ("allow", "deny"):
        raise ValueError(
            f"inbound_parent must be 'allow' or 'deny', got {inbound_parent!r}"
        )
    if lineage_group not in ("", "solitary"):
        raise ValueError(
            f"lineage_group must be '' or 'solitary', got {lineage_group!r}"
        )
    if not isinstance(may_spawn, bool):
        raise ValueError(f"may_spawn must be a bool, got {type(may_spawn).__name__}")
    if not isinstance(group_name, str):
        raise ValueError(f"group_name must be a str, got {type(group_name).__name__}")
    primary = group_name.strip()
    # A caller that names only the primary keeps its old meaning: the
    # stored set is {primary}. A caller that names the full set gets it
    # stored verbatim, with the primary folded in so the set is never a
    # strict subset of what the mesh already resolves.
    if group_names is None:
        encoded_groups = _join_group_names([primary])
    elif isinstance(group_names, str):
        # Reject BEFORE the splat below, which would silently expand a
        # string into its characters and store those as group names.
        encoded_groups = _join_group_names(group_names)
    else:
        encoded_groups = _join_group_names([*group_names, primary])
    from .state_db import open_db

    now = time.time()
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO node_comms_policy ("
            "name, outbound_siblings, outbound_parent, "
            "inbound_siblings, inbound_parent, lineage_group, "
            "may_spawn, group_name, group_names, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "outbound_siblings=excluded.outbound_siblings, "
            "outbound_parent=excluded.outbound_parent, "
            "inbound_siblings=excluded.inbound_siblings, "
            "inbound_parent=excluded.inbound_parent, "
            "lineage_group=excluded.lineage_group, "
            "may_spawn=excluded.may_spawn, "
            "group_name=excluded.group_name, "
            "group_names=excluded.group_names, "
            "updated_at=excluded.updated_at",
            (
                name,
                outbound_siblings,
                outbound_parent,
                inbound_siblings,
                inbound_parent,
                lineage_group,
                1 if may_spawn else 0,
                primary,
                encoded_groups,
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

    That invisibility is correct for POLICY EVALUATION — a caller asking
    "may this agent spawn?" wants an answer, not a lecture about rows.
    It is wrong for DIAGNOSIS: when an ACL refuses, "this agent is
    registered and ungrouped" and "this host has never heard of this
    agent" are different facts and the operator needs to know which.
    Use :func:`comms_policy_row_exists` for that; do NOT infer it from
    a returned value equal to the defaults, which is ambiguous by
    design.
    """
    if not name:
        return dict(DEFAULT_COMMS_POLICY)
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT outbound_siblings, outbound_parent, "
            "inbound_siblings, inbound_parent, lineage_group, may_spawn, "
            "group_name, group_names "
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
        "group_name": str(row["group_name"]),
        "group_names": _split_group_names(row["group_names"]),
    }


def comms_policy_row_exists(
    *,
    name: str,
    db_path: Path | None = None,
) -> bool:
    """True iff THIS store holds a policy row for ``name``.

    The narrow question :func:`read_comms_policy` deliberately hides, and
    the one a denial message needs.

    INCIDENT 2026-08-09: three agents were refused ``host_exec`` with
    "caller '<name>' resolves to group ''". That message asserts one
    cause — you are registered and ungrouped — when the truth was the
    other: the caller was being looked up in a store that had no row for
    it at all. Both produce the empty string, at two layers
    (``resolve_group_name`` collapses them, and so does this module's
    ``read_comms_policy``), each documented as intended. So the message
    sent three readers after their group labels instead of after WHICH
    DATABASE was consulted, and cost about fifteen minutes.

    This does NOT change any decision. Both cases still deny, and deny
    for the same reason. It exists so the denial can say which one it
    was.
    """
    if not name:
        return False
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM node_comms_policy WHERE name = ?",
            (name,),
        ).fetchone()
    return row is not None


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
    sender_parent = str(sender_parent_row["parent_name"]) if sender_parent_row else None
    target_parent = str(target_parent_row["parent_name"]) if target_parent_row else None
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
