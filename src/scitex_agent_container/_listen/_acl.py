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
    resolve_node_token,
    spawn_allowed,
)

log = logging.getLogger(__name__)

__all__ = [
    "AclDecision",
    "NodeAuthMiddleware",
    "check_send_acl",
    "check_spawn",
    "deny_response",
]


AclDecision = tuple[Literal["allow", "deny"], str | None]


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
    2. **Cross-group is denied by default.** The effective sender
       (authenticated_node, or claimed_from_agent for an
       administrative caller) must share a group with the target.
       Same-name (self-send) is trivially allowed.
    3. **Explicit cross-group grants flip a deny to allow** — see
       :func:`_state.state_db_nodes.grant_send`.
    4. The empty-sender case (no authenticated node AND no claimed
       from_agent) is denied — there is no identity to gate on.

    Returns ``("allow", None)`` or ``("deny", reason)``. The reason
    is suitable for inclusion in a 403 body and a host log line.
    """
    if not target:
        return ("deny", "missing target")

    # Determine the *effective* sender identity for the ACL check.
    if authenticated_node is not None:
        # Per-node bearer was presented.
        if (
            claimed_from_agent is not None
            and claimed_from_agent != authenticated_node
        ):
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

    sender_group = derive_group(name=sender, db_path=db_path)
    if target in sender_group:
        return ("allow", None)

    if has_grant(sender=sender, target=target, db_path=db_path):
        return ("allow", None)

    return (
        "deny",
        (
            f"cross-group send: sender {sender!r} "
            f"(group={sorted(sender_group)}) may not address {target!r} "
            "without an explicit ACL grant. Add a grant with "
            f"`grant_send(sender={sender!r}, target={target!r})` "
            "in state.db."
        ),
    )


def check_spawn(
    *,
    caller: str | None,
    db_path: Path | None = None,
) -> AclDecision:
    """Wrap :func:`spawn_allowed` in the same allow/deny tuple shape
    as :func:`check_send_acl` so the listen-server handler can branch
    uniformly.

    Current policy: root-only spawn. ``caller=None`` is the
    administrative / human-operator path (allowed).
    """
    allowed, reason = spawn_allowed(caller=caller, db_path=db_path)
    if allowed:
        return ("allow", None)
    return ("deny", reason)


def deny_response(reason: str) -> JSONResponse:
    """Standard 403 body for an ACL denial. Loud + structured.

    Logged at WARNING so the host operator sees the rejection in the
    listen-server log. Denial is the policy working — not a crash —
    but the sender must know exactly why (handoff §0 Hard rules).
    """
    log.warning("ACL deny: %s", reason)
    return JSONResponse(
        {"error": "ACL deny", "reason": reason}, status_code=403
    )
