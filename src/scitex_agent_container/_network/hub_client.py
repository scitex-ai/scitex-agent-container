"""Stdlib-only HTTP client for the Orochi hub's lead-state-handover API.

Three endpoints (server side: scitex-orochi PR feat/lead-state-handover-server):

  - POST /api/agents/<name>/snapshot/        — upsert payload (FR-A)
  - GET  /api/agents/<name>/snapshot/latest/ — fetch latest payload (FR-A)
  - GET  /api/agents/<name>/owner/           — current_host + priority_list +
                                               healthy{} (FR-B)

Auth: workspace token from ``SAC_HUB_TOKEN``. Hub base URL from
``SCITEX_AGENT_HUB_URL`` (defaults to ``https://scitex-orochi.com``).

Stdlib only — no requests/httpx dependency. Same urlopen pattern as
``scitex_agent_container.hooks._dispatch_http`` so this module is safe
to import at agent_start without dragging in heavy deps.

All functions are best-effort: network / HTTP errors are logged and
swallowed, returning ``None`` (or an empty result dict) so the caller
can decide whether the failure is fatal. Hot path: a hub outage must
NOT block agent_start / agent_stop.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

_DEFAULT_HUB = "https://scitex-orochi.com"
_HTTP_TIMEOUT_S = 10.0


def _hub_url() -> str:
    return os.environ.get("SCITEX_AGENT_HUB_URL", _DEFAULT_HUB).rstrip("/")


def _hub_token() -> str:
    return os.environ.get("SAC_HUB_TOKEN", "").strip()


def _request(method: str, path: str, *, body: dict | None = None) -> dict | None:
    """Issue a hub request. Returns parsed JSON dict or ``None`` on error."""
    token = _hub_token()
    if not token:
        logger.debug("hub_client: SAC_HUB_TOKEN unset, skipping %s %s", method, path)
        return None

    url = f"{_hub_url()}{path}"
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if method == "GET":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={urlparse.quote(token)}"
    else:
        payload = dict(body or {})
        payload["token"] = token
        data = json.dumps(payload, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url, data=data, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw)
    except urlerror.HTTPError as exc:
        # 404s are expected (no snapshot yet) — caller decides.
        body_preview = ""
        try:
            body_preview = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        logger.info(
            "hub_client: %s %s -> %s %s",
            method,
            path,
            exc.code,
            body_preview,
        )
        return None
    except (urlerror.URLError, OSError, ValueError) as exc:
        logger.warning("hub_client: %s %s failed: %s", method, path, exc)
        return None


def push_snapshot(
    agent_name: str,
    payload: dict[str, Any],
    *,
    owner_host: str = "",
) -> bool:
    """POST a snapshot for ``agent_name``. Returns True on 200."""
    body = {"payload": payload, "owner_host": owner_host}
    out = _request("POST", f"/api/agents/{agent_name}/snapshot/", body=body)
    if out is None:
        return False
    return out.get("status") == "ok"


def fetch_snapshot(agent_name: str) -> dict | None:
    """GET the latest snapshot. Returns the response dict or ``None``.

    Response shape: ``{"agent_name", "owner_host", "payload", "updated_at"}``.
    Returns ``None`` if the agent has no snapshot yet (404) or on transport
    error.
    """
    return _request("GET", f"/api/agents/{agent_name}/snapshot/latest/")


def fetch_owner(agent_name: str) -> dict:
    """GET the owner endpoint. Always returns a dict (empty on error).

    Response shape: ``{"agent", "current_host", "priority_list", "healthy"}``.
    """
    out = _request("GET", f"/api/agents/{agent_name}/owner/")
    if out is None:
        return {
            "agent": agent_name,
            "current_host": "",
            "priority_list": [],
            "healthy": {},
        }
    return out
