"""Named-group resolver for the group-based a2a ACL (operator 2026-06-25).

A SECOND grouping axis layered on top of the existing lineage-derived
group mesh (:func:`scitex_agent_container._state.state_db_nodes.derive_group`):

  * Each agent has a NAMED group. Source of truth is the spec
    ``metadata.labels``. The convention is the PLURAL list form
    ``groups: [<name>]`` — every agent ``spec.yaml`` and the operator's
    templates author the named group as a one-element list, e.g.
    ``groups: [researcher]``; the FIRST non-empty element is the named
    group. The SINGULAR string ``group: <name>`` is also accepted (and
    WINS over the plural list when both are present), for hand-written
    specs that prefer a scalar.
  * When NEITHER ``group`` nor ``groups`` is present, the group is
    *derived from the
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
    "GENERALIST_GROUP",
    "MESH_GROUPS",
    "PRIVILEGED_GROUP",
    "RESEARCHER_GROUP",
    "group_from_labels",
    "groups_mesh",
    "is_developer_group",
    "is_mesh_group",
    "resolve_group",
]


# The single privileged group name. Members get full agent-CRUD +
# ACL-CRUD authority (see :func:`._listen._acl.check_spawn` /
# ``check_lineage_acl``).
DEVELOPER_GROUP = "developer"

# The other two standard fleet groups. They carry NO extra authority
# (unlike ``developer``); they exist so a researcher / generalist agent
# resolves to a stable named group for the cross-group mesh below.
RESEARCHER_GROUP = "researcher"
GENERALIST_GROUP = "generalist"

# The privileged group (operator 2026-07-02): e.g. grant / dotfiles /
# channel-broker agents. Like ``developer`` it may broker arbitrary
# host-exec (see :data:`._listen._host_exec.ELIGIBLE_GROUPS`) and, by being
# a MESH group below, may manage agents across groups (start / restart /
# stop / delete / status / tail via :func:`._listen._acl.check_lineage_acl`)
# with no lineage edge and no per-pair grant — "work flexibly".
PRIVILEGED_GROUP = "privileged"


# Cross-group mesh (operator 2026-06-27; ``privileged`` added 2026-07-02):
# the STANDARD fleet groups coordinate with each other in all directions —
# a ``developer`` may address a ``researcher`` may address a ``generalist``
# may address a ``privileged``, no per-pair grant needed. This is
# "fleet-mesh by default" for the standard groups, the layer above the
# same-named-group mesh. Being a mesh group is ALSO what lets ``privileged``
# manage agents across groups (start / restart / stop / delete / status /
# tail) via :func:`._listen._acl.check_lineage_acl`.
#
# A group OUTSIDE this set does NOT mesh: e.g. a paper-scitex-clew solver
# in an isolated group (and/or ``lineage_group='solitary'`` + per-spec
# ``inbound.siblings=deny``) falls through to the explicit-grant ACL,
# preserving the solid isolation scientific rigor requires. To mesh a new
# group, add its name here (single-line edit, zero schema change).
MESH_GROUPS: frozenset[str] = frozenset(
    {DEVELOPER_GROUP, RESEARCHER_GROUP, GENERALIST_GROUP, PRIVILEGED_GROUP}
)


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


def _first_named_group(labels: dict) -> str | None:
    """Pull the operative named-group string out of a ``labels`` dict.

    Honours the two authored forms, SINGULAR-string-wins:

      1. ``labels["group"]`` (a string) — wins verbatim when present and
         non-empty (whitespace-trimmed).
      2. else the FIRST non-empty element of ``labels["groups"]`` (a
         list) — the spec convention (``groups: [researcher]``).

    Returns the chosen group string, or ``None`` when neither form
    yields a non-empty value. Defensive: ``groups`` may be absent, a
    non-list (e.g. a bare string or other scalar), or empty/all-blank —
    any of those is treated as "absent" and falls through to ``None``.
    """
    singular = labels.get("group")
    if singular is not None:
        trimmed = str(singular).strip()
        if trimmed:
            return trimmed
    plural = labels.get("groups")
    if isinstance(plural, (list, tuple)):
        for item in plural:
            if item is None:
                continue
            trimmed = str(item).strip()
            if trimmed:
                return trimmed
    return None


def group_from_labels(labels: dict | None) -> str:
    """Resolve the named group from a spec's ``metadata.labels`` dict.

    Convenience wrapper over :func:`resolve_group`. Precedence:

      1. SINGULAR ``labels["group"]`` (string) wins when non-empty.
      2. else the FIRST non-empty element of the PLURAL
         ``labels["groups"]`` list — the spec convention
         (``groups: [researcher]``).
      3. else role-derivation via :func:`resolve_group` (a
         developer-ish ``labels["role"]`` → :data:`DEVELOPER_GROUP`).
      4. else ``""`` (ungrouped).

    A missing / ``None`` labels dict yields ``""`` (ungrouped). The
    plural ``groups`` key is read defensively (absent / non-list /
    empty are all treated as "absent" — see :func:`_first_named_group`).
    """
    if not labels:
        return ""
    return resolve_group(
        group_label=_first_named_group(labels),
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


def is_mesh_group(group: str | None) -> bool:
    """Return True iff ``group`` is one of the standard mesh groups.

    Case-insensitive on the resolved group name. ``None`` / empty →
    False. Members of :data:`MESH_GROUPS` coordinate with members of any
    other mesh group (see :func:`groups_mesh`); a group outside the set
    does not mesh and stays isolated.
    """
    if not group:
        return False
    return str(group).strip().lower() in MESH_GROUPS


def groups_mesh(group_a: str | None, group_b: str | None) -> bool:
    """Return True iff ``group_a`` and ``group_b`` both mesh.

    The cross-group allow predicate: a send is meshed when BOTH the
    sender's and the target's resolved named groups are standard mesh
    groups (:data:`MESH_GROUPS`). Either side being ungrouped, or in a
    non-mesh group (e.g. an isolated solver group), returns False — the
    send then falls through to the explicit-grant ACL.
    """
    return is_mesh_group(group_a) and is_mesh_group(group_b)
