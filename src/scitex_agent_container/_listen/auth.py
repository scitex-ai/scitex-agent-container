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

Fork authority is deliberately not a bearer role: an independent owner token
arrives in ``X-SAC-Owner-Token`` and establishes ``host-owner`` on request
state. It is stored outside HOME and never injected into agent containers.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


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

    Admits the host-wide ``token`` passed at construction for ordinary access.
    An independent ``owner_token`` may additionally authenticate a host-owner
    principal. A second bearer branch admitted per-node tokens from ``node_tokens``
    until 2026-08-28; nothing ever minted one, so it never admitted a
    request (see the module docstring).

    Health endpoint at ``/v1/health`` is unauthenticated so monitors
    can probe liveness without provisioning credentials.
    """

    PUBLIC_PATHS = frozenset({"/v1/health"})

    def __init__(self, app, *, token: str, owner_token: str | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._token = token
        # This credential is deliberately separate from the shared listener
        # bearer injected into every agent container.  It authenticates the
        # bare-host owner principal used by security-sensitive fork creation.
        self._owner_token = owner_token

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        got = _extract_bearer(request)
        if got is None:
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        if not hmac.compare_digest(got, self._token):
            return JSONResponse({"error": "invalid bearer token"}, status_code=403)

        # Authority is transport-derived.  A body/header claim such as
        # ``authority=admin`` or ``X-SAC-Authority: admin`` has no effect.
        # Presence of a wrong owner credential is a hard refusal rather than a
        # downgrade, preventing typoed host invocations from silently running
        # with container-level authority.
        request.state.authenticated_principal = None
        supplied_owner = request.headers.get("x-sac-owner-token")
        if supplied_owner is not None:
            if self._owner_token is None or not hmac.compare_digest(
                supplied_owner, self._owner_token
            ):
                return JSONResponse(
                    {"error": "invalid owner credential"}, status_code=403
                )
            request.state.authenticated_principal = "host-owner"
        return await call_next(request)
