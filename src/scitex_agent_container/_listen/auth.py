"""Bearer-token auth middleware for ``sac listen``.

Per SAC_OROCHI_SCOPES.md §4.4: sac listen binds loopback or to a
private tunnel-only interface, so the bearer token is defense-in-depth
rather than the primary transport security. Constant-time comparison
to dodge timing oracles; clear 401/403 separation.
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
        if not hmac.compare_digest(got, self._token):
            return JSONResponse({"error": "invalid bearer token"}, status_code=403)
        return await call_next(request)
