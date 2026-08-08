"""In-container client for the ``POST /v1/host_exec`` bypass.

Used by the ``host_exec_local`` MCP tool so a developer/researcher agent can
run arbitrary commands on the host through the ``sac listen`` daemon. Mirrors
:mod:`._spawn_client` for URL/bearer resolution and error surface; only the
endpoint (``/v1/host_exec``) and payload shape (``argv`` + optional
``cwd``/``timeout_s``/``env``/``caller``) differ.

The ``opener`` parameter is a ``urllib.request.urlopen``-shaped callable seam;
tests inject a hand-rolled fake so there is no monkeypatching of the transport.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from ._listen_client_resolve import (
    _parse_body,
    _resolve_base_url,
    _resolve_bearer,
    _resolve_caller,
)

logger = logging.getLogger(__name__)

# Server-side per-command timeout CONTRACT (mirrors the listen ``/v1/host_exec``
# handler): when the caller omits ``timeout_s`` the server uses 300 s, and any
# ``timeout_s`` it accepts is clamped to ``(0, 3600]``. The client-side HTTP wait
# is DERIVED from this contract (effective server timeout + a margin) so the two
# stay consistent — see :func:`_resolve_http_timeout`.
_SERVER_DEFAULT_TIMEOUT_S: float = 300.0
_SERVER_MAX_TIMEOUT_S: float = 3600.0

# Margin added on top of the server-side deadline so the HTTP layer never chops
# off a long-but-progressing exec BEFORE the server's own timeout fires — while
# staying BOUNDED.
#
# Load-resilience fix (incident 2026-07-09): the pre-fix value was a FIXED
# 3700 s regardless of the requested ``timeout_s``. A jammed ``:7878`` listen
# (host load spike, load ~27 on 16 cores) then blocked this MCP tool handler for
# up to ~62 min on a single ``host_exec`` call. That long block timed out the
# stdio MCP client (Claude Code), which DROPPED the stdio connection — and Claude
# Code does not auto-reconnect a stdio MCP mid-session (only HTTP/SSE), so the
# host_exec/agent_* tools vanished for the rest of the session even though the
# server process stayed alive. Deriving the wait from the server contract keeps a
# blocked handler bounded (~330 s for the default) so the client never times the
# whole server out. See ``docs/mcp-load-resilience.md``.
_HTTP_TIMEOUT_MARGIN_S: float = 30.0

# Optional hard override of the derived client-side HTTP timeout (seconds).
_HTTP_TIMEOUT_ENV_VAR = "SAC_MCP_HOST_EXEC_HTTP_TIMEOUT_S"


def _resolve_http_timeout(
    timeout_s: float | None, explicit_http_timeout_s: float | None
) -> float:
    """Bound the client-side HTTP wait to the server-side deadline + margin.

    Resolution order:

    1. ``explicit_http_timeout_s`` — an explicit per-call override (a caller /
       test that knows better) wins verbatim.
    2. ``SAC_MCP_HOST_EXEC_HTTP_TIMEOUT_S`` env — a deployment-wide hard override.
    3. Derived: the *effective* server-side timeout (``timeout_s`` when given,
       else the 300 s server default), clamped to ``(0, 3600]``, plus
       :data:`_HTTP_TIMEOUT_MARGIN_S`.

    The derived path is the load-incident fix: it keeps the client wait just
    above what the server itself will honour (~330 s for the default, ~1830 s for
    ``timeout_s=1800``) instead of a fixed ~62 min, so a handler blocked on a
    jammed listen fails fast enough that the stdio MCP client never times the
    whole server out and drops it.
    """
    if explicit_http_timeout_s is not None:
        return explicit_http_timeout_s
    env_raw = os.environ.get(_HTTP_TIMEOUT_ENV_VAR, "").strip()
    if env_raw:
        try:
            return float(env_raw)
        except ValueError:
            logger.warning(
                "host_exec: ignoring invalid %s=%r (not a float seconds value)",
                _HTTP_TIMEOUT_ENV_VAR,
                env_raw,
            )
    effective = _SERVER_DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    effective = max(0.0, min(effective, _SERVER_MAX_TIMEOUT_S))
    return effective + _HTTP_TIMEOUT_MARGIN_S


class HostExecRequestError(Exception):
    """The host_exec POST reached the listen and was rejected (auth / ACL /
    validation / server error), or transport failed. ``status`` is the HTTP
    code when known; ``body`` is the parsed server body when the server
    returned one."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


__all__ = ["HostExecRequestError", "request_host_exec"]


def request_host_exec(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout_s: float | None = None,
    env: dict[str, str] | None = None,
    caller: str | None = None,
    base_url: str | None = None,
    bearer: str | None = None,
    http_timeout_s: float | None = None,
    opener: Callable | None = None,
) -> dict[str, Any]:
    """POST ``argv`` (+ optional ``cwd``/``timeout_s``/``env``/``caller``) to
    the listen daemon's ``/v1/host_exec`` and return the parsed JSON body.

    Raises :class:`HostExecRequestError` on any non-2xx response or transport
    failure — never returns a fake success. On a 2xx the body is the endpoint's
    contract (``{"exit_code", "stdout", "stderr", "duration_s", "timed_out"}``).

    ``http_timeout_s`` bounds the client-side wait. When ``None`` (the default) it
    is DERIVED from the server-side timeout contract via :func:`_resolve_http_timeout`
    (effective ``timeout_s`` clamped to ``(0, 3600]`` + margin, ~330 s for the
    default) rather than a fixed multi-minute wait — so a jammed listen cannot
    block this handler long enough for the stdio MCP client to drop the whole
    server (incident 2026-07-09, ``docs/mcp-load-resilience.md``).
    """
    resolved_http_timeout = _resolve_http_timeout(timeout_s, http_timeout_s)
    base = _resolve_base_url(base_url)
    tok = _resolve_bearer(bearer)

    body: dict[str, Any] = {"argv": argv}
    if cwd is not None:
        body["cwd"] = cwd
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    if env is not None:
        body["env"] = env
    # AUTO-RESOLVE the caller identity from SAC_NAME when one is not supplied.
    # Without this, an in-container agent that omits `caller` sends a body with
    # no identity at all; the server has no per-node bearer on the host-wide
    # bearer path either, so `check_group_acl` refuses with
    #   403 "host_exec requires a resolvable caller (per-node bearer or 'caller'
    #        body claim)"
    # even though SAC_NAME is set in that container. Reported by scitex-storage
    # 2026-07-28 (sac-listen-restart-defect-cluster) and hit again 2026-08-09.
    #
    # The tool's own documentation already PROMISED this behaviour — "defaults
    # to SAC_NAME from the container" — while no layer implemented it: the MCP
    # tool passes `caller` straight through and this client only forwarded a
    # non-None value. Documented-but-absent is worse than missing, because the
    # 403 then reads as a permissions problem rather than an unsent field.
    #
    # `_resolve_caller` keeps an explicit argument verbatim (including "" ->
    # None, the deliberate opt-out for the admin path), so this only fills the
    # gap and never overrides a caller's choice.
    resolved_caller = _resolve_caller(caller)
    if resolved_caller is not None:
        body["caller"] = resolved_caller

    payload = json.dumps(body).encode("utf-8")
    url = f"{base}/v1/host_exec"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urlrequest.Request(url, data=payload, method="POST", headers=headers)
    opener_fn = opener if opener is not None else urlrequest.urlopen

    try:
        with opener_fn(req, timeout=resolved_http_timeout) as resp:
            raw = resp.read()
    except urlerror.HTTPError as exc:
        raw_body = b""
        try:
            raw_body = exc.read() or b""
        except Exception:  # stx-allow: fallback (best-effort body read; status + URL are enough to surface)
            pass
        parsed = _parse_body(raw_body)
        raise HostExecRequestError(
            f"host_exec rejected: listen returned HTTP {exc.code} ({parsed!r})",
            status=exc.code,
            body=parsed,
        ) from exc
    except urlerror.URLError as exc:
        raise HostExecRequestError(
            f"host_exec transport error at {url}: {exc!s}"
        ) from exc

    return _parse_body(raw) or {}
