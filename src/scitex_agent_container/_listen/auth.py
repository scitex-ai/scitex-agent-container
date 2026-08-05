"""Bearer-token auth middleware for ``sac listen``.

sac listen binds loopback or to a
private tunnel-only interface, so the bearer token is defense-in-depth
rather than the primary transport security. Constant-time comparison
to dodge timing oracles; clear 401/403 separation.

**WI-2 update**: the middleware admits **either** the host-wide
token (administrative caller / cross-host-forwarder path) **or** a
per-node token registered in :mod:`_state.state_db_nodes`
(``node_tokens`` table). The inner
:class:`_listen._acl.NodeAuthMiddleware` then tags
``request.state.authenticated_node`` with the resolved name for the
ACL gate.
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

    Admits *either* the host-wide ``token`` (passed at construction)
    *or* a per-node token registered in ``node_tokens`` (WI-2).

    Health endpoint at ``/v1/health`` is unauthenticated so monitors
    can probe liveness without provisioning credentials.
    """

    PUBLIC_PATHS = frozenset({"/v1/health"})

    def __init__(self, app, *, token: str) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        got = _extract_bearer(request)
        if got is None:
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        # 1) Host-wide token — administrative / cross-host caller.
        if hmac.compare_digest(got, self._token):
            return await call_next(request)
        # 2) WI-2 per-node bearer — resolves against ``node_tokens``.
        #    Import lazily so the lazy-loading test fixtures (which
        #    patch ``state_db.DEFAULT_DB_PATH``) take effect.
        from .._state.state_db_nodes import resolve_node_token

        if resolve_node_token(token=got) is not None:
            return await call_next(request)
        return JSONResponse({"error": "invalid bearer token"}, status_code=403)
