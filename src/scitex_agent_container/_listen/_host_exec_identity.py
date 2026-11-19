"""Caller identity for ``POST /v1/host_exec``.

host_exec authorises by the CALLER's groups, so the caller name must come
from a credential, not from the request. The listen bearer is host-wide and
proves nothing about which agent sent a request; a ``caller`` field in the
body is a self-asserted claim. Resolution, strongest first:

1. ``agent-token`` — ``request.state.authenticated_agent``, set by the auth
   middleware from a per-agent token minted at agent start
   (:mod:`.agent_tokens`). A body ``caller`` that disagrees with it is
   REFUSED: the request is internally inconsistent, and honouring either
   side would be a guess.
2. ``node-bearer`` — ``request.state.authenticated_node`` (no current
   middleware sets it; kept so a future per-node credential slots in).
3. ``name-claimed (legacy, unauthenticated)`` — the body ``caller`` alone.
   MIGRATION ONLY: agents started before per-agent tokens existed receive
   theirs at their next normal start, and refusing them now would break
   running agents. Every such call is logged at WARNING and audited with
   this identity. Set :data:`REQUIRE_AGENT_TOKEN_ENV` to ``1`` on the
   ``sac listen`` process to refuse them; the default flips to refuse once
   the fleet has restarted onto token-carrying launches.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .._logging import get_logger

#: ``sac listen`` env switch: truthy = refuse name-claimed host_exec calls.
REQUIRE_AGENT_TOKEN_ENV = "SAC_HOST_EXEC_REQUIRE_AGENT_TOKEN"

IDENTITY_AGENT_TOKEN = "agent-token"
IDENTITY_NODE_BEARER = "node-bearer"
IDENTITY_NAME_CLAIMED = "name-claimed (legacy, unauthenticated)"
IDENTITY_UNRECOGNISED_TOKEN = (
    "name-claimed (legacy, unauthenticated; presented agent token not recognised)"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

logger = get_logger(__name__)


def require_agent_token_from_env() -> bool:
    """Read the refuse-name-claimed switch from the listen process env."""
    return os.environ.get(REQUIRE_AGENT_TOKEN_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class CallerIdentity:
    """Outcome of caller resolution.

    ``caller`` is the name the group gate is applied to (``None`` when
    nothing resolved). ``identity`` says how it was established — it goes
    into the audit line. ``refusal`` is a reason string when the call must
    be refused before the group gate runs.
    """

    caller: str | None
    identity: str
    refusal: str | None = None


def resolve_caller_identity(
    state: object,
    claimed_caller: str | None,
    *,
    require_agent_token: bool,
) -> CallerIdentity:
    """Decide who is calling, from the request state and the body claim."""
    agent = getattr(state, "authenticated_agent", None)
    if agent:
        if claimed_caller and claimed_caller != agent:
            logger.warning(
                f"host_exec refused: body caller {claimed_caller!r} disagrees "
                f"with the agent token's identity {agent!r}"
            )
            return CallerIdentity(
                caller=agent,
                identity=IDENTITY_AGENT_TOKEN,
                refusal=(
                    f"body 'caller' {claimed_caller!r} does not match the "
                    f"authenticated agent {agent!r}"
                ),
            )
        return CallerIdentity(caller=agent, identity=IDENTITY_AGENT_TOKEN)

    node = getattr(state, "authenticated_node", None)
    if node:
        return CallerIdentity(caller=node, identity=IDENTITY_NODE_BEARER)

    identity = (
        IDENTITY_UNRECOGNISED_TOKEN
        if getattr(state, "agent_token_presented", False)
        else IDENTITY_NAME_CLAIMED
    )
    if not claimed_caller:
        return CallerIdentity(
            caller=None,
            identity=identity,
            refusal=(
                "host_exec requires a resolvable caller (an agent token, or "
                "during migration a 'caller' body claim)"
            ),
        )
    if require_agent_token:
        logger.warning(
            f"host_exec refused: caller {claimed_caller!r} is {identity} and "
            f"{REQUIRE_AGENT_TOKEN_ENV} is set"
        )
        return CallerIdentity(
            caller=claimed_caller,
            identity=identity,
            refusal=(
                f"host_exec requires an agent token ({REQUIRE_AGENT_TOKEN_ENV} "
                f"is set); caller {claimed_caller!r} sent only a name claim. "
                "Restart the agent so it receives its token."
            ),
        )
    logger.warning(
        f"host_exec caller {claimed_caller!r} accepted on an unauthenticated "
        f"name claim ({identity}); it gets an agent token at its next start"
    )
    return CallerIdentity(caller=claimed_caller, identity=identity)


def refusal_audit_entry(
    resolved: CallerIdentity,
    claimed_caller: str | None,
    argv: list[str],
    cwd: str | None,
) -> dict[str, object]:
    """The audit line for a call refused on identity grounds (nothing ran)."""
    return {
        "ts": time.time(),
        "caller": resolved.caller,
        "claimed_caller": claimed_caller,
        "identity": resolved.identity,
        "argv": argv,
        "cwd": cwd,
        "refused": resolved.refusal,
    }


__all__ = [
    "IDENTITY_AGENT_TOKEN",
    "IDENTITY_NAME_CLAIMED",
    "IDENTITY_NODE_BEARER",
    "IDENTITY_UNRECOGNISED_TOKEN",
    "REQUIRE_AGENT_TOKEN_ENV",
    "CallerIdentity",
    "refusal_audit_entry",
    "require_agent_token_from_env",
    "resolve_caller_identity",
]
