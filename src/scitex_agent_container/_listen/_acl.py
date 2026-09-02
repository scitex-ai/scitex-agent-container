"""ACL gate + authenticated-identity middleware for ``sac listen``.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-21 directive (RESTORED the authenticated-identity criterion
the prior limited scope had deferred):

* "Group-based default ACL. Default policy: intra-group send is
  allowed — parent↔child *and* sibling↔sibling, bidirectional.
  Everything cross-group is denied until an explicit grant is added."

* **Authenticated sender identity** — REMOVED 2026-08-28. A
  ``NodeAuthMiddleware`` here resolved per-node bearer tokens out of
  the ``node_tokens`` table and pinned ``params.metadata.from_agent``
  against the resolved name. Nothing in ``src/`` ever minted such a
  token, so the table held 0 rows on every fleet host, no bearer ever
  resolved, and the anti-spoof branch in ``check_send_acl`` never
  once fired. Middleware, branch and table are gone together. What
  gates a send today — and did all along — is the host-wide bearer at
  the perimeter (:class:`_listen.auth.BearerAuthMiddleware`) plus the
  NAME-based predicates below.

* Cross-group grants are accepted (see :mod:`_state.state_db_nodes`
  ``grant_send`` / ``has_grant``); the sender for the grant check is
  the ``metadata.from_agent`` claim, honoured verbatim on the
  administrative / host-bearer path — which is every path.

* "Denial is **explicit**: a denied send returns a clear ``403`` to
  the sender and is logged."

The administrative-caller path (host-bearer) is the cross-host
forwarding seam: another sac listen forwards a peer's send to this
host by authenticating with the destination's host bearer (pulled
from the ``peer-tokens/<dest-host>.token`` registry on the
forwarder's side — see :mod:`_listen.peer_tokens`).
"""

from __future__ import annotations

import logging
from typing import Literal

from starlette.responses import JSONResponse

from .._state.state_db_nodes import (
    derive_group,
    has_grant,
    is_developer,
    read_comms_policy,
    resolve_group_name,
    same_named_group,
    sender_target_relationship,
    spawn_allowed,
)
from ..config._group_resolver import groups_mesh

log = logging.getLogger(__name__)

__all__ = [
    "AclDecision",
    "check_lineage_acl",
    "check_send_acl",
    "check_spawn",
    "deny_response",
]


def check_lineage_acl(
    *,
    caller: str | None,
    target: str,
) -> AclDecision:
    """Decide whether ``caller`` may operate on ``target`` via lineage.

    PR-3 Checkpoint 3 — the generalized ACL gate the DELETE,
    STATUS, send, and tail surfaces consume so an agent can only
    manage agents that are itself OR its lineage descendants
    (transitively, walked via the ``lineage`` table).

    Policy:

      * ``caller is None`` (or empty string) — administrative /
        operator path (the host-wide bearer). Always allowed. Mirrors
        the spawn-gate's admin treatment. Read that literally when
        reasoning about the HTTP surfaces: the DELETE and tail
        handlers derive ``caller`` ONLY from
        ``request.state.authenticated_node``, which the removed
        per-node-bearer middleware was the only thing that ever set,
        so those two routes pass ``None`` here on every request and
        this gate admits them. It was already so before the removal —
        the table had no rows — and this docstring now says it out
        loud instead of implying a second, non-admin caller shape
        that never arrived. The restart and host_exec handlers do
        still produce a non-``None`` caller, from the request body's
        ``caller`` field.
      * ``caller == target`` — self-management. A SAC agent
        managing its OWN runtime (e.g. ``sac agents status`` on
        itself) is always allowed regardless of lineage.
      * ``caller`` is in the ``developer`` group (operator
        2026-06-25) — full agent-CRUD authority over ANY target,
        independent of lineage. See
        :func:`_state.state_db_nodes.is_developer`.
      * ``target ∈ descendants_of(caller)`` — caller is a
        transitive ancestor; lineage-scoped operation permitted.
      * ``groups_mesh(caller, target)`` (operator 2026-06-29
        "agents manage agents") — caller and target BOTH resolve
        into the standard fleet mesh (developer / researcher /
        generalist). Manage authority is granted across these
        groups with no lineage edge and no per-pair grant, exactly
        as :func:`check_send_acl` meshes sends. A non-mesh group
        (isolated solver) stays unmanageable cross-group.
      * Otherwise — deny with a structured reason naming the
        caller, the target, and the fact that no lineage edge
        connects them.

    Returns the same ``("allow", None)`` / ``("deny", reason)``
    tuple shape as :func:`check_send_acl` / :func:`check_spawn`
    so callers can compose uniformly. The deny ``reason`` is
    suitable for inclusion in :func:`deny_response` (which now
    wraps it with ``kind="acl_deny"`` per the 5-kind contract).

    ``db_path`` is GONE (2026-08-28). It was "exposed so tests can
    isolate from the global state.db", and after the ``lineage`` move
    there is no state.db behind this gate to isolate from: the descendant
    walk reads the shared PostgreSQL store, which isolates via
    ``SCITEX_STORE_DSN`` (the ``pg_schema`` fixture). A parameter that
    still promised per-file isolation would promise something no lookup
    under it can deliver.
    """
    if caller is None or caller == "":
        return ("allow", None)
    if caller == target:
        return ("allow", None)
    # Developer group full authority (operator 2026-06-25): a caller in
    # the ``developer`` group may manage ANY agent (stop / restart /
    # delete / status / tail), not just its lineage descendants. Checked
    # before the descendant walk so developer-group CRUD never depends on
    # a lineage edge to the target.
    if is_developer(name=caller):
        return ("allow", None)
    from .._state._lineage import descendants_of

    descendants = descendants_of(name=caller)
    if target in descendants:
        return ("allow", None)
    # Standard-fleet manage mesh (operator 2026-06-29: "agents manage
    # agents"). This BROADENS manage authority beyond lineage: a caller
    # may also manage (stop / restart / delete / status / tail) a target
    # when BOTH resolve into the standard group mesh (developer /
    # researcher / generalist) — exactly the cross-group predicate
    # ``check_send_acl`` uses for sends. So a researcher (e.g. neurovista)
    # may restart a developer peer (e.g. scitex-todo) with no lineage edge
    # and no per-pair grant. A non-mesh group (e.g. an isolated solver)
    # is NOT meshed and stays unmanageable cross-group, preserving the
    # solid isolation scientific rigor requires.
    if groups_mesh(
        resolve_group_name(name=caller),
        resolve_group_name(name=target),
    ):
        return ("allow", None)
    return (
        "deny",
        (
            f"lineage ACL deny: caller {caller!r} has no lineage edge "
            f"to target {target!r}. Permitted operations are self "
            "(caller == target), any transitive descendant via the "
            "lineage table, any target when the caller is in the "
            "developer group, or any target when caller and target "
            "both belong to the standard fleet mesh (developer / "
            "researcher / generalist)."
        ),
    )


AclDecision = tuple[Literal["allow", "deny", "block"], str | None]


def check_send_acl(
    *,
    claimed_from_agent: str | None,
    target: str,
) -> AclDecision:
    """Decide whether a ``message:send`` should be admitted.

    Inputs:

    * ``claimed_from_agent`` — what ``params.metadata.from_agent``
      said. May be missing. Every caller reaching here holds the
      host-wide bearer (administrative / cross-host forwarding), so
      the claim is honoured verbatim — see item 1 below.
    * ``target`` — the ``<name>`` in
      ``POST /agents/<name>/message:send``.

    Decision logic:

    1. **There is no per-request identity to pin the claim against.**
       An ``authenticated_node`` parameter and the anti-spoof branch it
       fed lived here until 2026-08-28: when a per-node bearer was
       presented, ``claimed_from_agent`` had to match the name that
       bearer resolved to. Nothing ever minted a per-node bearer, so
       that parameter was ``None`` on every real call and the branch
       never fired; it is deleted rather than kept, because a
       signature promising spoof-proof identity is a promise this
       function cannot keep. The host-wide bearer at the perimeter is
       what authenticates a caller today.
    2. **Messaging is DEFAULT-ALLOW cross-group (operator 2026-07-03).**
       a2a messaging is collaboration, not a security boundary, so any
       working group may address any other. The same-group / mesh / grant
       predicates below still short-circuit to allow (byte-identical for
       grouped fleets), but a cross-group send with no shared group / mesh
       / grant now ALSO allows instead of denying. The security boundary
       stays on PRIVILEGED ACTIONS only, gated elsewhere and NOT touched
       here: host_exec (:data:`._host_exec.ELIGIBLE_GROUPS`) and lifecycle
       management (:func:`check_lineage_acl`). Same-name (self-send) is
       trivially allowed.
    3. **Overrides that STILL deny** are evaluated before the default
       allow: an explicit block (``state_db_blocks.has_block`` → "block")
       and a per-spec ``spec.comms`` parent/siblings=deny
       (:func:`_phase3_relationship_deny` → "deny"). An explicit
       ``grant_send`` remains a no-op-compatible allow.
    4. The empty-sender case (no claimed from_agent) is denied — there
       is no identity to gate on. A missing target is denied likewise.

    Returns ``("allow", None)``, ``("deny", reason)``, or ``("block",
    reason)``. The reason is suitable for a 403 body and a host log line.
    """
    if not target:
        return ("deny", "missing target")

    # Administrative / cross-host forwarding path — the only path. The
    # caller passed the host-wide bearer; we honour metadata.from_agent
    # verbatim — but it must be present.
    if not claimed_from_agent:
        return (
            "deny",
            (
                "no authenticated identity and no metadata.from_agent — "
                "cannot determine sender for ACL"
            ),
        )
    sender = claimed_from_agent

    if sender == target:
        return ("allow", None)

    # Task #27 — block check (block wins over grant). The receiver
    # explicitly silenced this sender at some prior decision; the
    # ACL gate honours it BEFORE the grant + cross-group checks so
    # an explicit veto wins even when a stale ``comms_grants`` row
    # would otherwise pass. The "block" decision value lets
    # :func:`node_message_send` distinguish silent-drop (no
    # receiver push, no approve-prompt re-fire) from the
    # cross-group deny that does push.
    from .._state.state_db_blocks import has_block as _has_block

    if _has_block(sender=sender, target=target):
        return ("block", f"blocked: {sender!r} → {target!r}")

    # Phase-3 (ADR-0010 Step 2) — per-spec outbound/inbound deny layered
    # on top of the group default. Restrictive only: a per-spec deny
    # blocks even when the group ACL would otherwise allow. Evaluated
    # BEFORE the group check so a sibling-deny on either side fires even
    # when sender and target share a group. Default policies (everything
    # ``allow``) leave the legacy group ACL semantics untouched.
    phase3_decision = _phase3_relationship_deny(sender=sender, target=target)
    if phase3_decision is not None:
        return phase3_decision

    sender_group = derive_group(name=sender)
    if target in sender_group:
        return ("allow", None)

    # Named-group mesh (operator 2026-06-25): a send is allowed when
    # sender and target resolve to the SAME non-empty NAMED group
    # (``metadata.labels.group`` / role-derived). This is the
    # "group-mesh by default" that replaces "per-pair grant by default"
    # for grouped fleets — e.g. every ``developer``-group agent may
    # address every other ``developer``-group agent with no per-pair
    # grant. Additive: an ungrouped fleet shares no named group and
    # falls through to the explicit-grant check below, unchanged.
    if same_named_group(sender=sender, target=target):
        return ("allow", None)

    # Cross-group mesh (operator 2026-06-27): the three STANDARD fleet
    # groups — developer / researcher / generalist — coordinate in all
    # directions with NO per-pair grant. Evaluated AFTER the phase-3
    # per-spec deny (so a solver's ``inbound.siblings=deny`` /
    # ``lineage_group='solitary'`` isolation still wins) and AFTER the
    # same-named-group mesh, but BEFORE the explicit-grant fallthrough.
    # An agent in a non-mesh group (e.g. an isolated solver group) is
    # NOT meshed and falls through to the grant check below, preserving
    # the solid isolation scientific rigor requires.
    if groups_mesh(
        resolve_group_name(name=sender),
        resolve_group_name(name=target),
    ):
        return ("allow", None)

    # Every lookup this function makes is now a PostgreSQL store read
    # isolating via SCITEX_STORE_DSN — the grants primitives since their
    # own move, and the lineage ones since 2026-08-28. The sentence that
    # stood here ("the other lookups in this function still take it, so
    # the parameter stays") was the last thing keeping ``db_path`` alive;
    # with it false, the parameter went too.
    if has_grant(sender=sender, target=target):
        return ("allow", None)

    # DEFAULT-ALLOW for cross-group a2a MESSAGING (operator 2026-07-03):
    # messaging is COLLABORATION, not a security boundary, so any working
    # group may address any other. The security boundary lives ONLY in
    # PRIVILEGED ACTIONS, gated elsewhere and untouched here — host_exec
    # (``._host_exec.ELIGIBLE_GROUPS``) and lifecycle management
    # (:func:`check_lineage_acl`). The overrides that still DENY a message
    # are evaluated ABOVE and preserved: an explicit block (``has_block``
    # → "block") and a per-spec ``spec.comms`` parent/siblings=deny
    # (:func:`_phase3_relationship_deny` → "deny"). Reaching here means
    # authenticated, unblocked, no per-spec deny, cross-group → ALLOW.
    return ("allow", None)


def _phase3_relationship_deny(
    *,
    sender: str,
    target: str,
) -> AclDecision | None:
    """Per-spec ACL deny based on the sender↔target lineage relationship.

    Returns a ``("deny", reason)`` tuple when either:

    * the SENDER's ``spec.comms.outbound`` denies the relationship from
      its side (``outbound.parent`` when target is sender's parent;
      ``outbound.siblings`` when target is sender's sibling), or
    * the TARGET's ``spec.comms.inbound`` denies the relationship from
      its side (``inbound.parent`` when sender is target's parent —
      i.e. target is sender's child; ``inbound.siblings`` when sender
      is target's sibling).

    Returns ``None`` when no per-spec rule applies — the caller falls
    through to the existing group/grant ACL.

    The relationship space (Phase-3 scope):

    * ``outbound.parent``  / ``inbound.parent``  — adjacent parent edge
    * ``outbound.siblings`` / ``inbound.siblings`` — shared-parent edge
    * (children are NOT modelled on either side — clew's gap list only
      asked for parent + sibling. A child→parent send is gated via the
      sender's ``outbound.parent`` on one side and the parent's
      ``inbound.parent`` on the other.)

    Defaults preserve current behaviour: every comb in the matrix
    starts ``"allow"`` so absence of ``spec.comms`` is a no-op here.
    """
    rel = sender_target_relationship(sender=sender, target=target)
    if rel in ("parent", "sibling"):
        sender_policy = read_comms_policy(name=sender)
        if rel == "parent" and sender_policy["outbound_parent"] == "deny":
            return (
                "deny",
                (
                    f"per-spec outbound deny: sender {sender!r} has "
                    "spec.comms.outbound.parent=deny; target "
                    f"{target!r} is its parent."
                ),
            )
        if rel == "sibling" and sender_policy["outbound_siblings"] == "deny":
            return (
                "deny",
                (
                    f"per-spec outbound deny: sender {sender!r} has "
                    "spec.comms.outbound.siblings=deny; target "
                    f"{target!r} is its sibling."
                ),
            )
    if rel in ("child", "sibling"):
        target_policy = read_comms_policy(name=target)
        if rel == "child" and target_policy["inbound_parent"] == "deny":
            return (
                "deny",
                (
                    f"per-spec inbound deny: target {target!r} has "
                    "spec.comms.inbound.parent=deny; sender "
                    f"{sender!r} is its parent."
                ),
            )
        if rel == "sibling" and target_policy["inbound_siblings"] == "deny":
            return (
                "deny",
                (
                    f"per-spec inbound deny: target {target!r} has "
                    "spec.comms.inbound.siblings=deny; sender "
                    f"{sender!r} is its sibling."
                ),
            )
    return None


def check_spawn(
    *,
    caller: str | None,
) -> AclDecision:
    """Wrap :func:`spawn_allowed` in the same allow/deny tuple shape
    as :func:`check_send_acl` so the listen-server handler can branch
    uniformly.

    **Read BOTH layers before concluding what is denied.** The
    ``is_developer`` branch below is a SHORT-CIRCUIT, not the policy —
    the RESEARCHER + PRIVILEGED allows live one level down, in
    :func:`spawn_allowed`. Reading only this file, and seeing
    ``is_developer`` alone, reads as "researchers fall through to the
    root-only gate". That is FALSE, and has been mis-triaged that way.
    Effective policy across both layers:

      * ``caller=None`` — administrative / operator path. Allowed.
      * ``developer`` group — allowed regardless of the root-only
        default, so a developer CHILD may spawn. Short-circuited here.
      * ``researcher`` / ``privileged`` — likewise allowed, one layer
        down in :func:`spawn_allowed` (dev AND research agents must
        both start/stop peers; denying privileged "is a sac bug").
      * ROOT node (no lineage parent) — allowed.
      * Any other child — DENIED: ``generalist`` / an isolated solver
        group / ungrouped. Generalist DOES mesh for *manage*
        (:func:`check_lineage_acl`) but gets NO spawn authority.
      * ``spec.lineage.may_spawn=false`` denies even a researcher —
        but NOT a developer, whose short-circuit here bypasses it.

    All three group checks are MEMBERSHIP over the caller's WHOLE
    named-group set, never primary-group equality — see
    :mod:`..._state.state_db_groups` (incident 2026-08-10, grant).
    """
    if caller and is_developer(name=caller):
        return ("allow", None)
    allowed, reason = spawn_allowed(caller=caller)
    if allowed:
        return ("allow", None)
    return ("deny", reason)


def deny_response(reason: str, *, kind: str = "acl_deny") -> JSONResponse:
    """Standard 403 body for an ACL denial. Loud + structured.

    Logged at WARNING so the host operator sees the rejection in the
    listen-server log. Denial is the policy working — not a crash —
    but the sender must know exactly why (handoff §0 Hard rules).

    Wire shape (PR-3 — pinned with clew, the 5th kind in the
    POST/DELETE/send/tail surface taxonomy):

    .. code-block:: json

       {
         "error":  "ACL deny",
         "kind":   "acl_deny",
         "reason": "<human-readable cause>"
       }

    Callers branch on ``kind`` (not prose); ``reason`` is for humans
    only. The kind defaults to ``"acl_deny"`` but is overridable so a
    future ACL phase (per-spec deny, lineage-out-of-scope) can shade
    the taxonomy without breaking the 5-kind contract.
    """
    log.warning("ACL deny: %s", reason)
    return JSONResponse(
        {"error": "ACL deny", "kind": kind, "reason": reason},
        status_code=403,
    )
