"""Live-runner forwarding for ``sac listen``.

Extracted from ``server.py`` to keep that module under the 512-line
cap. Single responsibility: given an :class:`AgentConfig` and a
prompt, POST to the live runner's per-agent A2A sidecar and return
the response (or ``None`` to signal the caller should fall back to
the heavier ``claude --resume`` re-launch path).

Port resolution order (introduced with the auto-allocator):

1. ``port_allocator.get_port(name)`` — the actual port the runner
   bound at start time, from state.db. This is the source of truth.
2. ``cfg.a2a.port`` if an explicit int — for legacy agents started
   before the allocator landed and never recorded.
3. None → no live runner; caller re-launches.

The ``"auto"`` sentinel on ``cfg.a2a.port`` is intentionally NOT
treated as a port — it only ever means "the start-time allocator
should pick one." A non-numeric value here means the agent was
never started, so there's nothing live to forward to.
"""

from __future__ import annotations

import asyncio
import json as _json
import urllib.error as _urlerror
import urllib.request as _urlrequest

from starlette.responses import JSONResponse

from .._state import port_allocator


async def forward_to_live_runner(
    cfg, name: str, prompt: str, options: dict, timeout: float = 600.0
) -> JSONResponse | None:
    """Push a prompt onto the live runner's inbox via its sidecar."""
    port = port_allocator.get_port(name)
    if not port:
        a2a = getattr(cfg, "a2a", None)
        raw = getattr(a2a, "port", None) if a2a else None
        if isinstance(raw, int) and raw > 0:
            port = raw
    if not port:
        return None
    a2a = getattr(cfg, "a2a", None)
    host = getattr(a2a, "host", None) or "127.0.0.1"

    url = f"http://{host}:{port}/v1/turn"
    body = _json.dumps({"text": prompt}).encode("utf-8")
    req = _urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _do_post() -> tuple[int, bytes]:
        try:
            with _urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, resp.read()
        except _urlerror.HTTPError as exc:
            return exc.code, exc.read()
        except _urlerror.URLError:
            return -1, b""

    status, payload = await asyncio.to_thread(_do_post)
    if status == -1:
        return None
    if status >= 400:
        return JSONResponse(
            {
                "name": name,
                "route": "live-runner",
                "status": status,
                "error": payload.decode("utf-8", "replace"),
            },
            status_code=status,
        )
    return JSONResponse(
        {
            "name": name,
            "route": "live-runner",
            "text": _json.loads(payload.decode("utf-8"))["text"],
        }
    )
