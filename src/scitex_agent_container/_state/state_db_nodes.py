"""Node-identity primitives for WI-2 ACL.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-21 directive (RESTORED the authenticated-identity criterion
the prior limited scope had deferred):

  * **Authenticated identity** — ``mint_node_token`` /
    ``resolve_node_token`` / ``list_node_tokens`` were re-exported here
    (from ``state_db_node_tokens``) until 2026-08-28, when the per-node
    bearer feature was removed: it had ZERO production callers, so the
    ``node_tokens`` table held 0 rows on every host and no bearer ever
    resolved to a name. The identity that actually gates today is the
    HOST-WIDE bearer plus the name-based ACL below. Names removed rather
    than left re-exported, for this module's usual reason: an importable
    ``mint_node_token`` promises a credential a caller could authenticate
    with, and nothing on the serving side would ever accept it.

  * **Group-based default ACL** — intra-group bidirectional,
    cross-group denied. Group is *derived from lineage*; see
    :func:`derive_group`.

  * **Cross-group grants** — accepted, keyed on the sender identity
    the caller claims in ``metadata.from_agent`` (the host-wide bearer
    is the only bearer there is; it is the administrative /
    cross-host-forwarder path and honours the claim verbatim). See
    :func:`grant_send`.

  * **Spawn permission** — a node with no parent may call
    ``sac agents start``; so may a child whose NAMED GROUPS INCLUDE
    ``developer``, ``researcher`` or ``privileged`` (operator ruling
    2026-07-05: "dev agents and research agents MUST have full
    permissions — including the ability to start/stop peer agents").
    Any other child is denied. See :func:`spawn_allowed`.

  * **Two group projections, one source.** ``group_name`` is the
    PRIMARY group — the single bucket the default-ACL mesh resolves
    through (:func:`resolve_group_name` / :func:`same_named_group`).
    ``group_names`` is the FULL set the spec authored, and it is what
    the AUTHORITY predicates read (:func:`is_developer` /
    :func:`is_researcher` / :func:`is_privileged`, in
    :mod:`.state_db_groups`). Both are written from the same
    ``metadata.labels`` at ``agent_start``. Collapsing authority onto
    the primary is what made ``grant`` — ``groups: [generalist,
    privileged, developer, researcher, active]`` — a non-developer to
    every gate while ``a2a_peers`` listed all five (2026-08-10).

The N-level structural capability of ``lineage`` is preserved —
nothing here hard-codes "2" or assumes fixed depth.

All times stored as ``REAL`` unix-seconds (float).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .state_db_acl_policy import (
    DEFAULT_COMMS_POLICY,
    apply_may_spawn_gate,
    comms_policy_row_exists,
    read_comms_policy,
    record_comms_policy,
    sender_target_relationship,
)
_logger = logging.getLogger(__name__)

__all__ = [
    "CommsNodeConflictError",
    "DEFAULT_COMMS_POLICY",
    "apply_may_spawn_gate",
    "comms_policy_row_exists",
    "derive_group",
    "grant_send",
    "has_grant",
    "in_named_group",
    "is_developer",
    "is_local_node",
    "is_privileged",
    "is_researcher",
    "list_comms_grants",
    "list_comms_nodes",
    "lookup_comms_node",
    "read_comms_policy",
    "record_comms_policy",
    "record_lineage",
    "register_comms_node",
    "rename_comms_node",
    "resolve_group_name",
    "resolve_group_names",
    "resolve_node_host",
    "revoke_send",
    "same_named_group",
    "sender_target_relationship",
    "spawn_allowed",
    "unregister_comms_node",
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
    """Record ``parent`` as ``child``'s parent (keep-first-parent).

    Idempotent; a child's parent is set once and immutable. A DIFFERENT
    parent KEEPS the existing one (logged, not raised) so a restart by a
    non-original-parent caller works in-place without re-parenting;
    identity drift stays impossible. Permission is gated upstream by
    ``check_spawn``.
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
            _logger.warning(
                "record_lineage: child %r keeps parent %r (ignored re-parent to %r)",
                child,
                existing["parent_name"],
                parent,
            )
            return
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
    policy = read_comms_policy(name=name)
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
# top of the lineage-derived group mesh above. The group NAME is resolved
# at agent_start from ``metadata.labels.group`` (else role-derived) and
# persisted in ``node_comms_policy.group_name``; these readers apply it at
# ACL-check time. Pure DB reads — the resolver itself is in
# :mod:`scitex_agent_container.config._group_resolver`.
# ---------------------------------------------------------------------------


# ``resolve_group_name`` (the PRIMARY / mesh projection) and
# ``same_named_group`` moved into the sibling group module alongside the
# MULTI-value readers: both projections now resolve through the SPEC
# first (operator 2026-08-12, "configuration → files under git"), and
# keeping the two in one file is what stops them drifting onto different
# sources again. Re-exported here so the long-standing import path
# ``from ..._state.state_db_nodes import resolve_group_name`` keeps working.


# The AUTHORITY predicates (``is_developer`` / ``is_researcher`` /
# ``is_privileged``) are MULTI-value and live in a sibling module under
# the per-file line cap. They ask "is <group> among this agent's named
# groups", NOT "is it the primary group" — see state_db_groups for why
# that distinction is the whole point (incident 2026-08-10, grant).
from .state_db_groups import (  # noqa: E402
    in_named_group,
    is_developer,
    is_privileged,
    is_researcher,
    resolve_group_name,
    resolve_group_names,
    same_named_group,
)

# ---------------------------------------------------------------------------
# spawn permission — root nodes, plus dev/research-role children
# ---------------------------------------------------------------------------


def spawn_allowed(
    *,
    caller: str | None,
    db_path: Path | None = None,
) -> tuple[bool, str | None]:
    """Decide whether ``caller`` is allowed to call ``sac agents start``.

    Current policy (handoff §4 / WI-2, relaxed per operator ruling
    2026-07-05): a *root* node (no parent) is allowed to spawn.
    A *child* is ALSO allowed when its named groups INCLUDE
    ``developer`` or ``researcher`` (:func:`is_developer` /
    :func:`is_researcher`) — the operator's exact words: "Dev agents
    and research agents MUST have full permissions — including the
    ability to start/stop peer agents." The ``privileged`` group is
    allowed on the same footing (operator ruling 2026-07-16: denying
    a privileged-group agent — dotfiles — "is a sac bug").
    Any other child is denied.
    ``caller=None`` means the administrative / human-operator path
    (e.g., a shell invocation from outside any sac-managed agent) —
    allowed.

    INCLUDE, not "equals the primary group" (incident 2026-08-10):
    every one of these three checks is a MEMBERSHIP test over the FULL
    set the spec authored, so authority never depends on where a group
    sits in a YAML list. ``grant``, whose spec says
    ``groups: [generalist, privileged, developer, researcher, active]``,
    was refused here for months because only ``generalist`` — the first
    element — ever reached the DB.

    Returns ``(True, None)`` on allow or ``(False, reason)`` on
    deny. The reason is suitable for inclusion in a 403 body and a
    host log line.

    **Per-spec override still applies**: every allow path here flows
    through :func:`apply_may_spawn_gate`, so ``spec.lineage.may_spawn
    = false`` still denies the caller even when its root/dev/research
    status would otherwise allow the spawn.
    """
    if caller is None or caller == "":
        # Admin / human operator. Skips the global root-only check;
        # per-spec may_spawn (Phase-3 Gap-5) layers on top.
        return apply_may_spawn_gate(caller=caller, base=(True, None))
    # The three spawn-authorised groups, checked as MEMBERSHIP over the
    # caller's whole named-group set. Hoisted above the lineage lookup
    # because the authority is lineage-independent: a developer- /
    # research- / privileged-group member may spawn or restart a peer to
    # self-heal a DOWN agent without waiting on the operator (operator
    # 2026-07-06 ACL incident). The per-spec may_spawn gate still layers
    # on top, exactly like the root path above.
    if (
        is_developer(name=caller)
        or is_researcher(name=caller)
        or is_privileged(name=caller)
    ):
        return apply_may_spawn_gate(caller=caller, base=(True, None))
    from .state_db import open_db

    with open_db(db_path) as conn:
        parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (caller,)
        ).fetchone()
    if parent_row is None:
        return apply_may_spawn_gate(caller=caller, base=(True, None))
    return (False, _spawn_denied_reason(caller, parent_row["parent_name"]))


def _spawn_denied_reason(caller: str, parent: str) -> str:
    """Compose the spawn-deny reason, naming the groups ACTUALLY resolved.

    The message this replaces asserted the caller "is in none of the
    developer, research, or privileged groups" — a claim about the
    AGENT. When the underlying bug made ``grant``'s ``developer`` label
    unreadable, that sentence was flatly false against the same server's
    own ``a2a_peers`` output, and it sent the reader after their group
    labels (which were correct) instead of after the reduction that
    dropped them. A denial must report what the GATE SAW, never assert
    what the agent is.

    So this states the resolved set verbatim, distinguishes "no policy
    row in this store at all" from "registered and ungrouped" (the
    2026-08-09 host_exec lesson — both produce an empty set and they are
    different facts), and names the command that re-publishes a stale
    row. Same decision, honest evidence.
    """
    from .state_db_acl_policy import comms_policy_row_exists

    groups = sorted(resolve_group_names(name=caller))
    if groups:
        seen = (
            f"the groups this host resolved for it are {groups}, none of "
            "which grant spawn authority"
        )
    elif comms_policy_row_exists(name=caller):
        seen = "it IS registered on this host but resolved to NO named group at all"
    else:
        seen = (
            "this host holds NO node_comms_policy row for it, so its groups "
            "could not be determined at all — which is not the same as being "
            "denied groups you hold; check WHICH state.db was consulted"
        )
    return (
        f"spawn denied: caller {caller!r} is a child of {parent!r} and "
        f"{seen}. Spawn requires one of: developer, researcher, privileged "
        "(membership — naming the group anywhere in metadata.labels.groups "
        "is enough), or being a root node. If the groups above disagree "
        "with the agent's spec.yaml, this host's row is STALE: run "
        "'sac agents refresh-acl' to re-publish it from the spec."
    )


# ---------------------------------------------------------------------------
# comms_grants — explicit cross-group send permissions. CRUD primitives live
# in a sibling module (state_db_grants) under the per-file line cap; re-exported
# here so the natural import path
# ``from ..._state.state_db_nodes import grant_send`` keeps working.
# ---------------------------------------------------------------------------

from .state_db_grants import (  # noqa: E402, F401
    grant_send,
    has_grant,
    list_comms_grants,
    revoke_send,
)

# ---------------------------------------------------------------------------
# WI-4 — name → host resolver primitives. Kept here (rather than
# splitting into a sibling module) because the cross-host forwarder
# consults both the resolver and the ACL primitives from this module.
# ---------------------------------------------------------------------------


def resolve_node_host(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Map a node ``name`` to ``{host, a2a_port}``.

    Resolution order (ADR-0014):

    1. ``instances`` table — the canonical "live agent" registry. Picks
       the most recently started live (``ended_at IS NULL``) row.
    2. the ADR-0014 ``comms_nodes`` directory — used for nodes that are
       NOT sac-managed agents (operator identities like ``lead``, peer
       hosts' listen-targets) and for agents registered on OTHER hosts.
       Since 2026-08-28 that is a read of the shared PostgreSQL store
       rather than of a local copy some earlier ``sac registry sync``
       may or may not have pulled.

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
            SELECT host, a2a_port, bound_port
              FROM instances
             WHERE name = ? AND ended_at IS NULL
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (name,),
        ).fetchone()
    if row is not None:
        # PREFER bound_port, fall back to the legacy a2a_port. Reading only
        # a2a_port discarded a usable address that was sitting in the same
        # row: `_send_resolve` has preferred bound_port over the legacy
        # column since it was introduced, and the writers populate BOTH
        # (`record_instance_start(a2a_port=bound, bound_port=bound)`), so a
        # row where only bound_port survived resolved to "no port" here and
        # 502'd at the forwarder while the sibling resolver would have
        # reached it. Same row, same moment, two answers — the asymmetry was
        # the defect, not the null.
        port = row["a2a_port"]
        if port is None:
            port = row["bound_port"]
        return {
            "host": str(row["host"]),
            "a2a_port": int(port) if port is not None else None,
        }
    # Fall through to comms_nodes (ADR-0014).
    #
    # NOT ALSO FALLING THROUGH WHEN THE ROW EXISTS BUT CARRIES NO PORT, and
    # the reason is that this function answers TWO questions with one value.
    # `is_local_node` consults it and reads ONLY ``host``: a live row means
    # "the agent is on that host", which stays true whether or not a port is
    # recorded. Falling through on a portless row would hand the locality
    # decision to ``comms_nodes``, which may name a DIFFERENT host — so an
    # agent that is genuinely local could start being forwarded away, and a
    # routing repair would have silently changed what "local" means.
    # Splitting locality from addressability is the real fix and it is a
    # bigger change than this one; see the a2a card.
    # No ``db_path``: since 2026-08-28 the directory is the shared PostgreSQL
    # store, not a table in this file. ``db_path`` still selects the SQLite
    # ``instances`` lookup above, which has not moved.
    return resolve_comms_node_host(name=name)


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
    rename_comms_node,
    unregister_comms_node,
)
