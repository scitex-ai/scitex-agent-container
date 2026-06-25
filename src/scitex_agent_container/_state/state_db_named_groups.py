"""Named-group ACL readers (operator 2026-06-25).

A SECOND grouping axis layered on top of the lineage-derived group mesh
in :mod:`.state_db_nodes`. The group NAME is resolved at ``agent_start``
from ``metadata.labels.group`` (else role-derived) by
:mod:`scitex_agent_container.config._group_resolver` and persisted in
``node_comms_policy.group_name`` (see :mod:`.state_db_acl_policy`); the
readers below apply it at ACL-check time.

Extracted into a sibling module so :mod:`.state_db_nodes` stays under the
per-file line cap, mirroring the existing ``state_db_acl_policy`` /
``state_db_comms_nodes`` split. The functions are re-exported from
``state_db_nodes`` so the import surface stays:

    from scitex_agent_container._state.state_db_nodes import (
        resolve_group_name, same_named_group, named_groups_peered,
        is_developer,
    )

All are pure DB reads (over ``read_comms_policy``); the resolver / peering
allowlist themselves live in
:mod:`scitex_agent_container.config._group_resolver`.
"""

from __future__ import annotations

from pathlib import Path

from .state_db_acl_policy import read_comms_policy

__all__ = [
    "is_developer",
    "named_groups_peered",
    "resolve_group_name",
    "same_named_group",
]


def resolve_group_name(
    *,
    name: str,
    db_path: Path | None = None,
) -> str:
    """Return ``name``'s persisted NAMED group, or ``""`` if ungrouped.

    Reads ``node_comms_policy.group_name`` (written at ``agent_start``
    from the resolved ``metadata.labels.group`` / role default). An
    agent with no policy row, or a row with an empty ``group_name``,
    is "ungrouped" and shares a named group with no one.
    """
    if not name:
        return ""
    policy = read_comms_policy(name=name, db_path=db_path)
    return str(policy.get("group_name", "") or "")


def same_named_group(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``sender`` and ``target`` share a NAMED group.

    Both groups must be NON-EMPTY and equal. Two ungrouped agents
    (empty group) do NOT match — that keeps absence byte-equivalent to
    the pre-group-name behaviour (an ungrouped fleet falls through to
    the lineage-mesh + explicit-grant ACL exactly as before).
    """
    sender_group = resolve_group_name(name=sender, db_path=db_path)
    if not sender_group:
        return False
    target_group = resolve_group_name(name=target, db_path=db_path)
    return target_group == sender_group


def named_groups_peered(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``sender`` and ``target`` are in PEERED groups.

    Cross-group PEERING (operator 2026-06-25): two DIFFERENT named
    groups that appear together in the
    :data:`scitex_agent_container.config._group_resolver._PEERED_GROUPS`
    allowlist may address each other in BOTH directions by default —
    e.g. ``scientist``↔``developer``. This lifts the cross-group
    default-DENY ONLY for the explicitly-paired groups; an unrelated
    third group stays denied (the allowlist is scoped, not blanket-
    open). Same-group sends are handled by :func:`same_named_group`,
    not here.
    """
    from ..config._group_resolver import groups_peered

    sender_group = resolve_group_name(name=sender, db_path=db_path)
    if not sender_group:
        return False
    target_group = resolve_group_name(name=target, db_path=db_path)
    return groups_peered(sender_group, target_group)


def is_developer(
    *,
    name: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``name``'s resolved NAMED group is ``developer``.

    The developer group has FULL AUTHORITY (operator 2026-06-25):
    members may CRUD agents (spawn / start / stop / restart / delete)
    and CRUD the ACL (grant / revoke). The spawn + lineage ACL gates
    consult this to short-circuit their default (root-only / lineage-
    descendant) checks.
    """
    from ..config._group_resolver import is_developer_group

    return is_developer_group(resolve_group_name(name=name, db_path=db_path))
