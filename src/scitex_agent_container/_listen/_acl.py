"""ACL gate + authenticated-identity middleware for ``sac listen``.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-21 directive (RESTORED the authenticated-identity criterion
the prior limited scope had deferred):

* "Group-based default ACL. Default policy: intra-group send is
  allowed — parent↔child *and* sibling↔sibling, bidirectional.
  Everything cross-group is denied until an explicit grant is added."

* **Authenticated sender identity** — per-node bearer tokens
  resolved by :class:`NodeAuthMiddleware`. ``check_send_acl``
  enforces "identity cannot be spoofed via a metadata field": when
  a per-node bearer is presented, ``params.metadata.from_agent``
  MUST match the bearer's resolved name; mismatch → 403.

* Cross-group grants are accepted (see :mod:`_state.state_db_nodes`
  ``grant_send`` / ``has_grant``); the sender for the grant check
  is the resolved-from-bearer name when a per-node bearer is
  presented, else (administrative / host-bearer caller) the
  ``metadata.from_agent`` claim.

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
from pathlib import Path
from typing import Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .._state.state_db_nodes import (
    derive_group,
    has_grant,
    is_developer,
    read_comms_policy,
    resolve_group_name,
    resolve_node_token,
    same_named_group,
    sender_target_relationship,
    spawn_allowed,
)
from ..config._group_resolver import groups_mesh

log = logging.getLogger(__name__)

__all__ = [
    "AclDecision",
    "NodeAuthMiddleware",
    "check_lineage_acl",
    "check_send_acl",
    "check_spawn",
    "deny_response",
]


def check_lineage_acl(
    *,
    caller: str | None,
    target: str,
    db_path: Path | None = None,
) -> AclDecision:
    """Decide whether ``caller`` may operate on ``target`` via lineage.

    PR-3 Checkpoint 3 — the generalized ACL gate the DELETE,
    STATUS, send, and tail surfaces consume so an agent can only
    manage agents that are itself OR its lineage descendants
    (transitively, walked via the ``lineage`` table).

    Policy:

      * ``caller is None`` (or empty string) — administrative /
        operator path (host-wide bearer, not a per-node token).
        Always allowed. Mirrors the spawn-gate's admin treatment.
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

    ``db_path`` is exposed so tests can isolate from the global
    state.db.
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
    if is_developer(name=caller, db_path=db_path):
        return ("allow", None)
    from .._state._lineage import descendants_of

    descendants = descendants_of(name=caller, db_path=db_path)
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
        resolve_group_name(name=caller, db_path=db_path),
        resolve_group_name(name=target, db_path=db_path),
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


class NodeAuthMiddleware:
    """Resolve the incoming Bearer token to a node identity, if any.

    Sits **after** the outer
    :class:`scitex_agent_container._listen.auth.BearerAuthMiddleware`
    — that middleware enforces *some* valid bearer was presented;
    this one resolves it to an identity:

    * If the bearer equals the host-wide token, attach
      ``request.state.authenticated_node = None`` to mark it as the
      administrative caller (cross-host forwarding path: the
      caller is a peer sac listen acting on behalf of a remote
      node; ``metadata.from_agent`` is honoured verbatim).
    * If the bearer matches a row in ``node_tokens``, attach the
      resolved node name.
    * Otherwise leave ``authenticated_node = None``. The outer
      middleware would already have rejected an unknown bearer, so
      this branch is defence-in-depth.

    ``db_path`` is exposed so tests can drop in an isolated
    state.db without touching ``$SCITEX_AGENT_CONTAINER_STATE_DB``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        host_bearer: str,
        db_path: Path | None = None,
    ) -> None:
        self.app = app
        self.host_bearer = host_bearer
        self.db_path = db_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("ascii", "replace")
        bearer = ""
        if auth.startswith("Bearer "):
            bearer = auth[len("Bearer ") :].strip()
        scope.setdefault("state", {})
        if bearer and bearer != self.host_bearer:
            resolved = resolve_node_token(token=bearer, db_path=self.db_path)
            scope["state"]["authenticated_node"] = resolved
        else:
            scope["state"]["authenticated_node"] = None
        await self.app(scope, receive, send)


def check_send_acl(
    *,
    authenticated_node: str | None,
    claimed_from_agent: str | None,
    target: str,
    db_path: Path | None = None,
) -> AclDecision:
    """Decide whether a ``message:send`` should be admitted.

    Inputs:

    * ``authenticated_node`` — resolved from the Bearer token by
      :class:`NodeAuthMiddleware`. ``None`` means the host-wide
      bearer was used (administrative / cross-host forwarding) — the
      caller is trusted to honour ``claimed_from_agent`` verbatim.
    * ``claimed_from_agent`` — what ``params.metadata.from_agent``
      said. May be missing.
    * ``target`` — the ``<name>`` in
      ``POST /agents/<name>/message:send``.

    Decision logic (handoff §4 acceptance):

    1. **Identity cannot be spoofed via a metadata field.** When a
       per-node bearer is presented, ``claimed_from_agent`` (if
       present) MUST match ``authenticated_node``; mismatch → deny.
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
    4. The empty-sender case (no authenticated node AND no claimed
       from_agent) is denied — there is no identity to gate on. Identity
       spoof + missing target are denied likewise.

    Returns ``("allow", None)``, ``("deny", reason)``, or ``("block",
    reason)``. The reason is suitable for a 403 body and a host log line.
    """
    if not target:
        return ("deny", "missing target")

    # Determine the *effective* sender identity for the ACL check.
    if authenticated_node is not None:
        # Per-node bearer was presented.
        if claimed_from_agent is not None and claimed_from_agent != authenticated_node:
            return (
                "deny",
                (
                    f"identity spoof: bearer authenticates "
                    f"{authenticated_node!r} but metadata.from_agent "
                    f"claims {claimed_from_agent!r}"
                ),
            )
        sender = authenticated_node
    else:
        # Administrative / cross-host forwarding path. The caller
        # passed the host-wide bearer; we honour
        # metadata.from_agent verbatim — but it must be present.
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

    if _has_block(sender=sender, target=target, db_path=db_path):
        return ("block", f"blocked: {sender!r} → {target!r}")

    # Phase-3 (ADR-0010 Step 2) — per-spec outbound/inbound deny layered
    # on top of the group default. Restrictive only: a per-spec deny
    # blocks even when the group ACL would otherwise allow. Evaluated
    # BEFORE the group check so a sibling-deny on either side fires even
    # when sender and target share a group. Default policies (everything
    # ``allow``) leave the legacy group ACL semantics untouched.
    phase3_decision = _phase3_relationship_deny(
        sender=sender, target=target, db_path=db_path
    )
    if phase3_decision is not None:
        return phase3_decision

    sender_group = derive_group(name=sender, db_path=db_path)
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
    if same_named_group(sender=sender, target=target, db_path=db_path):
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
        resolve_group_name(name=sender, db_path=db_path),
        resolve_group_name(name=target, db_path=db_path),
    ):
        return ("allow", None)

    if has_grant(sender=sender, target=target, db_path=db_path):
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
    db_path: Path | None,
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
    rel = sender_target_relationship(sender=sender, target=target, db_path=db_path)
    if rel in ("parent", "sibling"):
        sender_policy = read_comms_policy(name=sender, db_path=db_path)
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
        target_policy = read_comms_policy(name=target, db_path=db_path)
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
    db_path: Path | None = None,
) -> AclDecision:
    """Wrap :func:`spawn_allowed` in the same allow/deny tuple shape
    as :func:`check_send_acl` so the listen-server handler can branch
    uniformly.

    **Read BOTH layers before concluding what is denied.** The
    ``is_developer`` branch below is a SHORT-CIRCUIT, not the policy —
    the RESEARCHER allow lives one level down, in :func:`spawn_allowed`.
    Reading only this file, and seeing ``is_developer`` with no
    ``is_researcher`` beside it, reads as "researchers fall through to
    the root-only gate". That is FALSE, and has been mis-triaged that
    way. Effective policy across both layers:

      * ``caller=None`` — administrative / operator path. Allowed.
      * ``developer`` group — allowed regardless of the root-only
        default, so a developer CHILD may spawn. Short-circuited here.
      * ``researcher`` group — likewise allowed, resolved one layer
        down in :func:`spawn_allowed` (operator ruling: dev AND
        research agents must both be able to start/stop peers).
      * ROOT node (no lineage parent) — allowed.
      * Any other child — DENIED: ``generalist`` / ``privileged`` /
        an isolated solver group / ungrouped. Note generalist and
        privileged DO mesh for *manage* (:func:`check_lineage_acl`)
        but get NO spawn authority; only developer + researcher do.
      * ``spec.lineage.may_spawn=false`` denies even a researcher —
        but NOT a developer, whose short-circuit here bypasses
        :func:`spawn_allowed` and the ``may_spawn`` gate with it. An
        agent that must never spawn has to stay out of ``developer``.
    """
    if caller and is_developer(name=caller, db_path=db_path):
        return ("allow", None)
    allowed, reason = spawn_allowed(caller=caller, db_path=db_path)
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
