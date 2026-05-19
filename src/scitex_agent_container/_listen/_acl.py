"""ACL gate on ``message:send`` for ``sac listen`` (WI-2, limited scope).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2) and the lead's
2026-05-20 directive (limited scope — defer authenticated identity):

* "Group-based default ACL. Default policy: intra-group send is
  allowed — parent↔child *and* sibling↔sibling, bidirectional.
  Everything cross-group is denied until an explicit grant is added."

* Cross-group grants are accepted (see :mod:`_state.state_db_nodes`
  ``grant_send`` / ``has_grant``); each grant row carries the audit
  caveat "trusts metadata.from_agent until per-node creds land".

* "Denial is **explicit**: a denied send returns a clear ``403`` to
  the sender and is logged."

**Deferred** (separate handoff filed in scitex-lead at
``GITIGNORED/FUTURE/sac-per-node-authenticated-acl.md``):

* Authenticated per-node identity. The current
  :func:`check_send_acl` gates on the self-claimed
  ``metadata.from_agent`` field. The cryptographic-identity follow-on
  will replace the input here with a token-resolved name; the
  decision logic stays the same.
* Identity-cannot-be-spoofed enforcement. Same gap, same follow-on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from starlette.responses import JSONResponse

from .._state.state_db_nodes import derive_group, has_grant, spawn_allowed

log = logging.getLogger(__name__)

__all__ = [
    "AclDecision",
    "check_send_acl",
    "check_spawn",
    "deny_response",
]


AclDecision = tuple[Literal["allow", "deny"], str | None]


def check_send_acl(
    *,
    claimed_from_agent: str | None,
    target: str,
    db_path: Path | None = None,
) -> AclDecision:
    """Decide whether a ``message:send`` should be admitted.

    Decision logic (handoff §4 acceptance, limited scope):

    1. Empty sender (no ``metadata.from_agent``) → deny — there is
       no identity to gate on.
    2. ``sender == target`` (self-send) → allow.
    3. ``target`` in ``derive_group(sender)`` (intra-group) → allow.
    4. Explicit cross-group grant (``has_grant(sender, target)``)
       → allow.
    5. Otherwise → deny with explanatory reason.

    Returns ``("allow", None)`` or ``("deny", reason)``. The reason
    is suitable for inclusion in a 403 body and a host log line.

    **Identity caveat** — ``claimed_from_agent`` is the self-claimed
    ``metadata.from_agent`` field. The cryptographic-identity
    follow-on will replace this input with a token-resolved name;
    the decision branches above will not change. See the module
    docstring for the deferred-work pointer.
    """
    if not target:
        return ("deny", "missing target")
    if not claimed_from_agent:
        return (
            "deny",
            (
                "no metadata.from_agent — cannot determine sender for ACL. "
                "Until per-node credentials land, every message:send MUST "
                "carry params.metadata.from_agent."
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
    return JSONResponse({"error": "ACL deny", "reason": reason}, status_code=403)
