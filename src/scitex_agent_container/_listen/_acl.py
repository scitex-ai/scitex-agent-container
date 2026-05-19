"""ACL check + authenticated-identity middleware for ``sac listen`` (WI-2).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2 "ACL: permissioned
messaging"):

* "Group-based default ACL. Default policy: intra-group send is
  allowed — parent↔child *and* sibling↔sibling, bidirectional.
  Everything cross-group is denied until an explicit grant is added."
* "Authenticated sender identity. ... Do not gate on an
  unauthenticated string."
* "Denial is **explicit**: a denied send returns a clear `403` to
  the sender and is logged".

This module supplies two layers:

* :class:`NodeAuthMiddleware` — resolves the incoming
  ``Authorization: Bearer <token>`` against the ``node_tokens``
  table (see :mod:`_state.state_db_nodes`). On success it attaches
  ``request.state.authenticated_node = <name>`` so downstream
  handlers know who is speaking. The middleware sits **after** the
  outer :class:`BearerAuthMiddleware`; the host-wide bearer admits
  any request and leaves ``authenticated_node`` unset
  (administrative / cross-host forwarding path). A per-node bearer
  pins identity to one name.

* :func:`check_send_acl` — gating function called by
  ``node_message_send``. Returns ``("allow", None)`` or
  ``("deny", reason)``. The reasons are surfaced verbatim in the
  403 body and in the host log — denial is the policy working, not
  a bug, but the sender must know exactly why.

Spawn permission (also called out by WI-2) lives in a sibling
``spawn_allowed`` helper alongside the lineage primitives —
co-located with the lineage data it consults.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .._state.state_db_nodes import derive_group, resolve_node_token

log = logging.getLogger(__name__)

__all__ = [
    "NodeAuthMiddleware",
    "check_send_acl",
    "AclDecision",
]


AclDecision = tuple[Literal["allow", "deny"], str | None]


class NodeAuthMiddleware:
    """Resolve the incoming Bearer token to a node identity, if any.

    Sits **after** :class:`scitex_agent_container._listen.auth.BearerAuthMiddleware`
    — that middleware already enforces that *some* valid bearer was
    presented. We add identity resolution on top:

    * If the bearer equals the host-wide token (the only bearer the
      outer middleware admits today), attach
      ``request.state.authenticated_node = None`` to mark it as the
      administrative caller.
    * If the bearer matches a row in ``node_tokens``, attach the
      node name.
    * Otherwise leave ``authenticated_node`` as ``None`` (the outer
      middleware would have rejected an unknown bearer; this branch
      is defence-in-depth).

    Configurable ``db_path`` so tests can drop in an isolated state.db
    without touching ``$SCITEX_AGENT_CONTAINER_STATE_DB``.
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
      caller is trusted to honour the ``claimed_from_agent`` field
      verbatim.
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
       Same-name (self-send) is trivially allowed — a node can
       always address itself.
    3. The empty-sender case (no authenticated node AND no claimed
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
                    f"identity spoof: bearer authenticates {authenticated_node!r} "
                    f"but metadata.from_agent claims {claimed_from_agent!r}"
                ),
            )
        sender = authenticated_node
    else:
        # Administrative / cross-host forwarding path. The caller
        # passed the host-wide bearer; we honour metadata.from_agent
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

    sender_group = derive_group(name=sender, db_path=db_path)
    if target in sender_group:
        return ("allow", None)

    return (
        "deny",
        (
            f"cross-group send: sender {sender!r} (group={sorted(sender_group)}) "
            f"may not address {target!r} without an explicit ACL grant"
        ),
    )


def deny_response(reason: str) -> JSONResponse:
    """Standard 403 body for an ACL denial. Loud + structured.

    Logged at WARNING so the host operator sees the rejection in the
    listen-server log. Denial is the policy working — not a crash —
    but the sender must know exactly why (handoff §0 Hard rules).
    """
    log.warning("ACL deny: %s", reason)
    return JSONResponse({"error": "ACL deny", "reason": reason}, status_code=403)
