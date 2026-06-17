"""Fetch + parse the `/agents` registry for ``sac a2a list`` — fail-loud, no traceback.

scitex-dev handoff (2026-06-17): on the Spartan runner host (bm159, fresh env,
``~/.local/bin/sac``) ``sac a2a list --json`` exited 1 with an unhandled
TRACEBACK, so scitex-todo's ``/fleet/mesh`` view (which shells out to it) 500'd
and blocked CI. Root cause: the inline request in ``a2a_group.a2a_list`` only
caught ``urllib.error.URLError`` — a socket timeout (``TimeoutError``) on a
loaded runner, or a non-JSON body, escaped as a raw traceback.

This module extracts the network + parse step behind a DI ``opener`` seam so it
is unit-testable without a real server, and maps EVERY failure mode
(connection refused / DNS / HTTP error / timeout / OS error / non-JSON / odd
shape) to a single :class:`A2aListError`. The caller turns that into one clean
``SystemExit`` line — fail-loud, no silent fallback, and never a raw traceback
that breaks a machine-readable caller.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable


class A2aListError(Exception):
    """Clean, message-carrying failure for ``sac a2a list`` (→ ``SystemExit``)."""


def parse_agents_response(raw: Any) -> list:
    """Parse a ``/agents`` response body into the agents list. Fail-loud.

    Accepts ``bytes`` or ``str``. Raises :class:`A2aListError` on a non-JSON
    body, a non-object payload, or an ``agents`` field that is not a list —
    so a 502 HTML page / truncated body / schema drift fails with a clear
    message instead of a downstream ``KeyError``/``JSONDecodeError`` traceback.
    """
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:  # JSONDecodeError ⊂ ValueError
        raise A2aListError(
            f"listen returned a non-JSON /agents body ({type(exc).__name__}: {exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise A2aListError(
            f"listen returned a non-object /agents body ({type(payload).__name__})"
        )
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        raise A2aListError(
            f"listen /agents 'agents' field is not a list ({type(agents).__name__})"
        )
    return agents


def fetch_agents(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 6.0,
) -> list:
    """``GET <url>/agents`` with the bearer token; return the agents list.

    Maps every reach/read failure — ``URLError`` (incl. ``HTTPError``),
    ``TimeoutError``/``socket.timeout``, and any other ``OSError`` — to
    :class:`A2aListError`, then delegates body parsing to
    :func:`parse_agents_response` (which fail-louds on bad JSON/shape). The
    result: ``sac a2a list`` never emits a raw traceback on a degraded host.

    ``opener`` is a DI seam (default :func:`urllib.request.urlopen`) so the
    error mapping is unit-testable with no real network.
    """
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url.rstrip("/") + "/agents")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise A2aListError(f"cannot reach listen at {url}: {exc}") from exc
    except (TimeoutError, OSError) as exc:  # socket.timeout, conn reset, ...
        raise A2aListError(
            f"listen at {url} did not respond ({type(exc).__name__}: {exc})"
        ) from exc
    return parse_agents_response(raw)


__all__ = ["A2aListError", "fetch_agents", "parse_agents_response"]
