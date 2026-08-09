"""Node-identity primitives for WI-2 ACL.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-21 directive (RESTORED the authenticated-identity criterion
the prior limited scope had deferred):

  * **Authenticated identity** — per-node bearer tokens minted at
    registration (:func:`mint_node_token`). The listen server resolves
    an incoming ``Authorization: Bearer <token>`` to a node name
    (:func:`resolve_node_token`). With this in place,
    ``check_send_acl`` enforces "identity cannot be spoofed via a
    metadata field" — when a per-node bearer is presented,
    ``params.metadata.from_agent`` MUST match the bearer's resolved
    name; mismatch → 403.

  * **Group-based default ACL** — intra-group bidirectional,
    cross-group denied. Group is *derived from lineage*; see
    :func:`derive_group`.

  * **Cross-group grants** — accepted, keyed on the *resolved*
    sender identity (per-node bearer authenticates the sender; the
    host-wide bearer is the administrative / cross-host-forwarder
    path, which honours ``metadata.from_agent`` verbatim). See
    :func:`grant_send`.

  * **Spawn permission** — a node with no parent may call
    ``sac agents start``; so may a child whose resolved NAMED group
    is ``developer`` or ``researcher`` (operator ruling 2026-07-05:
    "dev agents and research agents MUST have full permissions —
    including the ability to start/stop peer agents"). Any other
    child is denied. See :func:`spawn_allowed`.

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
    read_comms_policy,
    record_comms_policy,
    sender_target_relationship,
)
from .state_db_node_tokens import (
    list_node_tokens,
    mint_node_token,
    resolve_node_token,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "CommsNodeConflictError",
    "DEFAULT_COMMS_POLICY",
    "apply_may_spawn_gate",
    "derive_group",
    "grant_send",
    "has_grant",
    "is_developer",
    "is_local_node",
    "is_researcher",
    "list_comms_grants",
    "list_comms_nodes",
    "list_node_tokens",
    "lookup_comms_node",
    "mint_node_token",
    "read_comms_policy",
    "record_comms_policy",
    "record_lineage",
    "register_comms_node",
    "resolve_group_name",
    "resolve_node_host",
    "resolve_node_token",
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
    policy = read_comms_policy(name=name, db_path=db_path)
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
# named groups + role predicates — EXTRACTED to .state_db_groups (this module
# was 587 lines, over the 512 budget). Re-exported here so every existing
# `from ...state_db_nodes import resolve_group_name` keeps working.
#
# state_db_groups also adds `resolve_group`, which returns the group AND its
# provenance — use that one wherever the answer reaches a human, because a
# bare "" cannot say whether an agent is ungrouped or has no policy row.
# ---------------------------------------------------------------------------

from .state_db_groups import (  # noqa: E402,F401
    GROUP_SOURCES,
    GroupResolution,
    is_developer,
    is_researcher,
    resolve_group,
    resolve_group_name,
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
    A *child* is ALSO allowed when its resolved NAMED group is
    ``developer`` or ``researcher`` (:func:`is_developer` /
    :func:`is_researcher`) — the operator's exact words: "Dev agents
    and research agents MUST have full permissions — including the
    ability to start/stop peer agents." The ``privileged`` group is
    allowed on the same footing (operator ruling 2026-07-16: denying
    a privileged-group agent — dotfiles — "is a sac bug"; checked in
    the group fallthrough below, which every named path also reaches).
    Any other child is denied.
    ``caller=None`` means the administrative / human-operator path
    (e.g., a shell invocation from outside any sac-managed agent) —
    allowed.

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
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
    if is_developer(name=caller, db_path=db_path) or is_researcher(
        name=caller, db_path=db_path
    ):
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
    from .state_db import open_db

    with open_db(db_path) as conn:
        parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (caller,)
        ).fetchone()
    if parent_row is None:
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
    # Child node: denied by default, EXCEPT a developer- or research-group
    # member may spawn / restart a peer regardless of parent/child lineage
    # (operator 2026-07-06 ACL incident — a research child such as neurovista
    # must be able to self-heal a DOWN peer like scitex-clew without waiting
    # on the operator). The per-spec may_spawn gate still layers on top,
    # exactly like the root path above.
    from ..config._group_resolver import (
        is_developer_group,
        is_privileged_group,
        is_research_group,
    )

    group = resolve_group_name(name=caller, db_path=db_path)
    if (
        is_developer_group(group)
        or is_research_group(group)
        or is_privileged_group(group)
    ):
        return apply_may_spawn_gate(caller=caller, base=(True, None), db_path=db_path)
    return (
        False,
        (
            f"spawn denied: caller {caller!r} is a child of "
            f"{parent_row['parent_name']!r} and is in none of the developer, "
            "research, or privileged groups. Current policy: only root "
            "nodes, or developer/research/privileged group members, may "
            "spawn (handoff §4 'lift-able policy' — a single edit to "
            "spawn_allowed())."
        ),
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
    unregister_comms_node,
)
