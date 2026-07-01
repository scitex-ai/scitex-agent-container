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
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from ._spawn_client import _parse_body, _resolve_base_url, _resolve_bearer

# ``sac image build`` can take many minutes; keep this comfortably above the
# server-side per-command default (300s) so the http layer never chops off a
# long-but-progressing exec before the server's own timeout fires.
_DEFAULT_HTTP_TIMEOUT_S: float = 3700.0


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
    http_timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S,
    opener: Callable | None = None,
) -> dict[str, Any]:
    """POST ``argv`` (+ optional ``cwd``/``timeout_s``/``env``/``caller``) to
    the listen daemon's ``/v1/host_exec`` and return the parsed JSON body.

    Raises :class:`HostExecRequestError` on any non-2xx response or transport
    failure — never returns a fake success. On a 2xx the body is the endpoint's
    contract (``{"exit_code", "stdout", "stderr", "duration_s", "timed_out"}``).
    """
    base = _resolve_base_url(base_url)
    tok = _resolve_bearer(bearer)

    body: dict[str, Any] = {"argv": argv}
    if cwd is not None:
        body["cwd"] = cwd
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    if env is not None:
        body["env"] = env
    if caller is not None:
        body["caller"] = caller

    payload = json.dumps(body).encode("utf-8")
    url = f"{base}/v1/host_exec"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urlrequest.Request(url, data=payload, method="POST", headers=headers)
    opener_fn = opener if opener is not None else urlrequest.urlopen

    try:
        with opener_fn(req, timeout=http_timeout_s) as resp:
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
