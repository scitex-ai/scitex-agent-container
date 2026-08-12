"""The ``sac listen`` half of the CI feedback rail: how a verdict reaches
a running agent, and how this rail knows whether anyone is listening.

Companion to :mod:`ci_card_rail`, which owns the card contract and the
CLI. This module owns exactly one thing: talking to the ``sac listen``
control plane over loopback.

WHY THERE IS NO WEBHOOK HERE (ADR-0024 §4.1). ``sac listen`` binds
``127.0.0.1`` only, and that is policy rather than accident — the CLI
refuses a non-loopback bind without an explicit override. So there is no
public route for GitHub to POST to, and the two mechanisms usually
reached for are both wrong here: a webhook cannot arrive, and a poller
is what you build when the signal cannot reach you. It can. The
self-hosted runner long-polls OUTWARD to GitHub and then executes the
job ON THIS HOST, where loopback is reachable and the bearer token is a
local file. The GitHub → host path already exists and is already
trusted; this module just uses it.

WHY DELIVERY GOES OVER THIS RAIL AND NOT THE CARD SIDECAR (ADR-0024 §2).
There are two notification rails in this fleet with opposite health. The
card package's file sidecar was measured frozen — 149 unseen events,
nothing moving since 07:05. sac's ``channel_events`` + a2a bus was
measured live, durable and replayable. Routing over the live one also
decouples this rail from the sidecar's unfinished retirement, so neither
piece of work blocks the other.

DEPENDENCIES: standard library only. This runs under ``uv run --with
scitex-cards`` on a runner that has no ``scitex_agent_container``
install, so every sac fact is fetched over HTTP rather than imported.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LISTEN_BASE_URL = "http://127.0.0.1:7878"
TOKEN_DIR = Path(".scitex") / "agent-container" / "tokens"

__all__ = [
    "DEFAULT_LISTEN_BASE_URL",
    "TOKEN_DIR",
    "fleet_agents",
    "listen_base_url",
    "listen_token",
    "notify_agent",
    "reachability_of",
]


def listen_base_url() -> str:
    return (os.environ.get("SAC_LISTEN_BASE_URL") or DEFAULT_LISTEN_BASE_URL).rstrip(
        "/"
    )


def listen_token() -> str | None:
    """The host-wide ``sac listen`` bearer, from env or its canonical file.

    Mirrors ``scitex_agent_container._listen.tokens.default_token_path``
    by construction rather than by import, because the runner's Python
    has no sac install. The env override exists so the same script works
    from a container that mounts the token elsewhere.
    """
    env = os.environ.get("SAC_LISTEN_BEARER") or os.environ.get("SAC_LISTEN_TOKEN")
    if env and env.strip():
        return env.strip()
    host = os.environ.get("SAC_LISTEN_TOKEN_HOST") or socket.gethostname()
    path = Path.home() / TOKEN_DIR / f"listen-{host}.token"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _request(
    path: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> Any:
    url = f"{listen_base_url()}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "{}")


def fleet_agents(token: str) -> list[dict[str, Any]]:
    """``GET /agents`` — every registered agent WITH its live reachability.

    The ``inbox_subscribers`` / ``inbox_reachable`` fields are the whole
    reason this call exists. REGISTERED IS NOT REACHABLE: an agent row
    can carry a pid, an a2a port and an ``active`` group while holding
    zero inbox subscribers, in which case a message to it lands on an
    empty bus and NO ERROR IS RAISED ANYWHERE. Measured on this host
    2026-08-12: 9 of 15 registered agents were in exactly that state,
    and every reachable one had been started within the previous eight
    hours — subscription reach decays with uptime. Choosing a recipient
    without reading this field is how a rail gets declared working while
    delivering to nobody.
    """
    body = _request("/agents", token=token)
    if isinstance(body, dict):
        agents = body.get("agents")
        if isinstance(agents, list):
            return [a for a in agents if isinstance(a, dict)]
    return []


def reachability_of(agents: list[dict[str, Any]], name: str) -> tuple[str, int]:
    """``(inbox_reachable, inbox_subscribers)`` for ``name``.

    ``("unknown", 0)`` when the name is not in the roster at all — which
    is a different fact from "known and deaf", and the caller reports it
    differently.
    """
    for agent in agents:
        if agent.get("name") == name:
            return (
                str(agent.get("inbox_reachable") or "unknown"),
                int(agent.get("inbox_subscribers") or 0),
            )
    return "unknown", 0


def notify_agent(
    token: str, *, agent: str, body: str, card_id: str | None = None
) -> dict[str, Any]:
    """``POST /v1/notify`` — persist to ``channel_events``, then publish.

    DURABLE BEFORE LIVE, and that ordering is the reason this endpoint is
    the right seam. The daemon writes the event to ``channel_events``
    BEFORE publishing it to the in-memory bus, and the persisted row id
    becomes the SSE ``id:`` line — so an agent that happens to be
    disconnected at this instant replays the event from its
    ``Last-Event-ID`` cursor on reconnect instead of losing it. The bus
    is a fan-out in front of a durable log, not the log itself.

    Delivery is also OUTBOUND-SUBSCRIPTION, never an inbound POST: a
    containerized agent's own ``turn_url`` is unreachable from outside
    its container, so it subscribes outward and the daemon publishes
    down that stream.

    Returns the daemon's JSON, including ``delivered_subscriber_count``.
    Raises ``urllib`` errors, which the caller must surface loudly.
    """
    return _request(
        "/v1/notify",
        token=token,
        payload={
            "agent": agent,
            "body": body,
            "card_id": card_id,
            "from_agent": "ci",
        },
    )


# EOF
