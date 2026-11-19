"""Bearer-token auth middleware for ``sac listen``.

sac listen binds loopback or to a
private tunnel-only interface, so the bearer token is defense-in-depth
rather than the primary transport security. Constant-time comparison
to dodge timing oracles; clear 401/403 separation.

**2026-08-28**: the HOST-WIDE token is the only bearer. A second
branch here admitted a per-node token from the ``node_tokens`` table
(WI-2); that table was never written by anything in ``src/`` — 0 rows
on every fleet host — so the branch could only ever fall through to
the 403 below. Table, primitives and branch removed together. Sender
identity now comes from ``metadata.from_agent``, gated by
:func:`_listen._acl.check_send_acl` on the name.

**Per-agent identity**: a request that ALSO carries the
:data:`.agent_tokens.AGENT_TOKEN_HEADER` header has that token resolved to
the agent it was minted for (:mod:`.agent_tokens`). The result is exposed
as ``request.state.authenticated_agent`` (the name, or ``None``) plus
``request.state.agent_token_presented`` (whether a header was sent at
all). An unrecognised agent token does NOT fail the request here — the
host bearer still authenticated it — so each identity-sensitive handler
decides what an unresolved identity means for it (see ``_host_exec``).
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .agent_tokens import AGENT_TOKEN_HEADER, resolve_agent_token


def _extract_bearer(request: Request) -> str | None:
    """Pull the bearer token out of the Authorization header."""
    auth = request.headers.get("authorization", "")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects requests missing or with a wrong bearer token.

    Admits the host-wide ``token`` passed at construction, and nothing
    else. A second branch admitted per-node tokens from ``node_tokens``
    until 2026-08-28; nothing ever minted one, so it never admitted a
    request (see the module docstring).

    Health endpoint at ``/v1/health`` is unauthenticated so monitors
    can probe liveness without provisioning credentials.
    """

    PUBLIC_PATHS = frozenset({"/v1/health"})

    def __init__(self, app, *, token: str) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        got = _extract_bearer(request)
        authenticated = got is not None and hmac.compare_digest(got, self._token)
        # Public health stays public. Only verified host-bearer requests may
        # discover typed mutation capabilities through that same health route.
        request.state.host_bearer_authenticated = authenticated
        # Per-agent identity is only ever derived on top of a verified host
        # bearer; an unauthenticated request never gets a resolved agent.
        agent_token = request.headers.get(AGENT_TOKEN_HEADER, "").strip()
        request.state.agent_token_presented = bool(agent_token)
        request.state.authenticated_agent = (
            resolve_agent_token(agent_token) if authenticated and agent_token else None
        )
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        if got is None:
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        # Host-wide token — administrative / cross-host caller. The only
        # bearer there is; a second branch resolving ``node_tokens`` was
        # removed 2026-08-28 (never minted, never resolved).
        if authenticated:
            return await call_next(request)
        return JSONResponse({"error": "invalid bearer token"}, status_code=403)
