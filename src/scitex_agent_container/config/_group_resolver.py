"""Named-group resolver for the group-based a2a ACL (operator 2026-06-25).

A SECOND grouping axis layered on top of the existing lineage-derived
group mesh (:func:`scitex_agent_container._state.state_db_nodes.derive_group`):

  * Each agent has a NAMED group. Source of truth is the spec label
    ``metadata.labels.group``.
  * When the ``group`` label is ABSENT, the group is *derived from the
    role* (``metadata.labels.role`` / ``CLAUDE_AGENT_ROLE``): the
    developer-ish roles — ``project-maintainer`` / ``maintainer`` /
    ``dev-agent`` / ``contributor`` (and their project-suffixed forms,
    e.g. ``contributor-figrecipe``) — resolve to the group
    :data:`DEVELOPER_GROUP`. So every existing dev agent joins
    ``developer`` with NO spec edit; an explicit ``labels.group``
    always overrides the role default.
  * Anything else (no group label, non-developer / absent role) →
    the empty string ``""`` — "ungrouped". An ungrouped agent never
    shares a NAMED group with anyone (the ACL same-group allow is
    gated on a non-empty match), so absence is byte-equivalent to the
    pre-existing behaviour.

This module is PURE (no DB, no I/O) — string in, string out — so it is
trivially testable without fixtures. The persistence half (writing the
resolved group into ``node_comms_policy`` at ``agent_start`` and reading
it back at ACL-check time) lives in
:mod:`scitex_agent_container._state.state_db_acl_policy` /
:mod:`scitex_agent_container._state.state_db_nodes`.

The developer-role set deliberately mirrors the long-lived-coordinator
set in :mod:`._session_continuity` (the same ``project-maintainer`` /
``maintainer`` / ``dev-agent`` / ``contributor`` strings the operator
named), but is kept SEPARATE: continuity is "does this role keep its
conversation across restarts", group-authority is "does this role
belong to the developer group". The two policies happen to overlap
today but answer different questions and must be free to diverge.
"""

from __future__ import annotations

__all__ = [
    "DEVELOPER_GROUP",
    "SCIENTIST_GROUP",
    "group_from_labels",
    "groups_peered",
    "is_developer_group",
    "resolve_group",
]


# The single privileged group name. Members get full agent-CRUD +
# ACL-CRUD authority (see :func:`._listen._acl.check_spawn` /
# ``check_lineage_acl``).
DEVELOPER_GROUP = "developer"

# The scientist group (operator 2026-06-25). Scientist agents author
# papers (paper-scitex-clew / paper-neurovista / paper-ripple-wm carry
# ``metadata.labels.group: scientist``) and need to collaborate with the
# developer fleet without a per-pair grant. Same-group sends are already
# covered by the named-group mesh; the cross-group reach is the peering
# allowlist below.
SCIENTIST_GROUP = "scientist"


# Cross-group PEERING allowlist (operator 2026-06-25). Each entry is a
# *frozenset of two group names* that may address each other in BOTH
# directions by default — the cross-group default-DENY is lifted only
# for these explicitly-paired groups, NOT blanket-opened. Two groups
# that do not co-occur in any entry stay default-denied (an explicit
# ``grant_send`` is still required between them).
_PEERED_GROUPS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({SCIENTIST_GROUP, DEVELOPER_GROUP}),
    }
)


def groups_peered(group_a: str | None, group_b: str | None) -> bool:
    """Return True iff ``group_a`` and ``group_b`` are a PEERED pair.

    Peering is symmetric (a↔b is the same entry as b↔a) and scoped to
    the explicit :data:`_PEERED_GROUPS` allowlist — currently only
    ``scientist``↔``developer``. Both group names must be non-empty;
    an ungrouped agent (empty group) is never peered. A group is NOT
    peered with itself here — same-group sends are the named-group
    mesh's job, kept separate so the two policies can diverge.

    Case-insensitive, whitespace-trimmed on both names.
    """
    if not group_a or not group_b:
        return False
    norm_a = str(group_a).strip().lower()
    norm_b = str(group_b).strip().lower()
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return False
    return frozenset({norm_a, norm_b}) in _PEERED_GROUPS


# Exact role strings (case-insensitive) that default to the developer
# group when no explicit ``labels.group`` is set. Operator-specified
# 2026-06-25.
_DEVELOPER_ROLES: frozenset[str] = frozenset(
    {
        "project-maintainer",
        "maintainer",
        "dev-agent",
        "contributor",
    }
)

# Role PREFIXES that also map to the developer group even when the role
# is project-suffixed — e.g. ``contributor-figrecipe``,
# ``dev-agent-clew``, ``maintainer-scitex``. Mirrors the
# project-suffix convention the continuity resolver already honours.
_DEVELOPER_ROLE_PREFIXES: tuple[str, ...] = (
    "project-maintainer-",
    "maintainer-",
    "dev-agent-",
    "contributor-",
)


def _role_is_developer(role: str | None) -> bool:
    """Return True iff ``role`` is a developer-ish role.

    Case-insensitive, whitespace-trimmed. Matches the exact role set
    or any project-suffixed prefix. ``None`` / empty → False.
    """
    if not role:
        return False
    norm = str(role).strip().lower()
    if not norm:
        return False
    if norm in _DEVELOPER_ROLES:
        return True
    return norm.startswith(_DEVELOPER_ROLE_PREFIXES)


def resolve_group(*, group_label: str | None, role: str | None) -> str:
    """Resolve an agent's NAMED group from its group label + role.

    Precedence (operator 2026-06-25):

      1. An explicit, non-empty ``group_label`` wins verbatim
         (whitespace-trimmed) — the operator can name ANY group, not
         just ``developer``.
      2. Otherwise, if ``role`` is developer-ish, the group is
         :data:`DEVELOPER_GROUP`.
      3. Otherwise, the empty string ``""`` (ungrouped).

    Returns a plain ``str`` (never ``None``). An empty return means
    "no named group" — the ACL same-group allow is gated on a
    NON-EMPTY match, so an ungrouped agent shares a named group with
    no one (absence is a no-op, fully backward-compatible).
    """
    if group_label is not None:
        trimmed = str(group_label).strip()
        if trimmed:
            return trimmed
    if _role_is_developer(role):
        return DEVELOPER_GROUP
    return ""


def group_from_labels(labels: dict[str, str] | None) -> str:
    """Resolve the named group from a spec's ``metadata.labels`` dict.

    Convenience wrapper over :func:`resolve_group` that pulls the
    ``group`` and ``role`` keys out of the labels mapping. A missing /
    ``None`` labels dict yields ``""`` (ungrouped).
    """
    if not labels:
        return ""
    return resolve_group(
        group_label=labels.get("group"),
        role=labels.get("role"),
    )


def is_developer_group(group: str | None) -> bool:
    """Return True iff ``group`` is the privileged developer group.

    Case-insensitive on the resolved group name. ``None`` / empty →
    False. Used by the spawn / lineage ACL gates to grant the
    developer group full agent-CRUD authority.
    """
    if not group:
        return False
    return str(group).strip().lower() == DEVELOPER_GROUP
