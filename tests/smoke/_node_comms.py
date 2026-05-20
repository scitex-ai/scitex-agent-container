"""Shared helpers for the node-comms smoke layer.

Both ``test_node_comms_e2e_http.py`` (raw HTTP/SSE transport) and
``test_node_comms_e2e_mcp.py`` (the ``a2a_*`` MCP tool layer) drive
the *same* substrate — a real ``sac listen`` on a loopback port, a
real ``state.db``, and real per-node bearer tokens. The wiring that
brings that substrate up (lineage + tokens + uvicorn + the A2A
JSON-RPC body shape + SSE consumers) lives here so neither test file
duplicates it.

Leading-underscore filename ⇒ pytest does not collect it as a test
module (``python_files = ["test_*.py"]``). Imported as
``from tests.smoke._node_comms import ...`` (the repo already imports
sibling helpers this way — see ``tests/e2e/test_agent_lifecycle.py``).

The shared **fixtures** (``disk_tmp``, ``comms_env``) live in
``conftest.py`` so pytest auto-discovers them; only plain functions
live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import yaml

from scitex_agent_container._state.state_db_nodes import (
    mint_node_token,
    record_lineage,
)

# ---------------------------------------------------------------------------
# uvicorn loopback helpers (mirrors tests/.../_listen/test_server.py).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind a loopback socket to port 0; return the OS-assigned port."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin uvicorn on ``127.0.0.1:port`` in a daemon thread.

    Teardown sets ``should_exit`` + joins; the ``finally`` fires even
    on test failure so a hung subscriber never strands the server.
    """
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn loopback did not start in 5s")
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# A2A JSON-RPC body shape — the same payload the SDK / MCP tool emits.
# ---------------------------------------------------------------------------


def _send_payload(text: str, *, from_agent: str | None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage"}
    message = {
        "message_id": "m-smoke",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    params: dict = {"message": message}
    if from_agent is not None:
        params["metadata"] = {"from_agent": from_agent}
    msg["params"] = params
    return msg


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# SSE consumer helpers
# ---------------------------------------------------------------------------


async def _await_subscribed_and_read_one(
    url: str, *, headers: dict, ready: asyncio.Event, deadline_s: float = 5.0
) -> dict:
    """Open SSE, signal ``ready`` on the first comment frame, then
    return the first ``data:`` payload as JSON."""
    async with httpx.AsyncClient(timeout=deadline_s) as ac:
        async with ac.stream("GET", url, headers=headers) as sse:
            async for line in sse.aiter_lines():
                if line.startswith(":"):
                    ready.set()
                    continue
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


async def _consume_event_with_id(url: str) -> tuple[str | None, dict]:
    """Open SSE (unauthenticated — used against a2a/_server.py), return
    ``(id_line_value, parsed_data)`` for the first event."""
    seen_id: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("id:"):
                    seen_id = line[len("id:") :].strip()
                    continue
                if line.startswith("data:"):
                    return seen_id, json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


# ---------------------------------------------------------------------------
# Listen-server bring-up: lineage + tokens for a fixed cast.
# ---------------------------------------------------------------------------


def _set_up_two_groups(db: Path) -> dict[str, str]:
    """Group A = parent_a + {alpha, beta}. Group B = parent_b + {gamma}.

    Mints a per-node bearer for every name (so each node can
    authenticate from its own session). Returns ``{name: token}``.
    """
    record_lineage(child="alpha", parent="parent_a", db_path=db)
    record_lineage(child="beta", parent="parent_a", db_path=db)
    record_lineage(child="gamma", parent="parent_b", db_path=db)
    return {
        "host": "smoke-host-token",
        "parent_a": mint_node_token(name="parent_a", db_path=db),
        "alpha": mint_node_token(name="alpha", db_path=db),
        "beta": mint_node_token(name="beta", db_path=db),
        "parent_b": mint_node_token(name="parent_b", db_path=db),
        "gamma": mint_node_token(name="gamma", db_path=db),
    }


def _set_up_four_siblings(db: Path) -> dict[str, str]:
    """One parent + four siblings: alpha, beta, gamma, zeta (group A only).

    Distinct cast from ``_set_up_two_groups`` so the fan-out case does
    not conflict with gamma's group-B placement there.
    """
    for child in ("alpha", "beta", "gamma", "zeta"):
        record_lineage(child=child, parent="parent_a", db_path=db)
    tokens = {"host": "smoke-host-token"}
    for name in ("parent_a", "alpha", "beta", "gamma", "zeta"):
        tokens[name] = mint_node_token(name=name, db_path=db)
    return tokens


# ---------------------------------------------------------------------------
# a2a/_server.py echo-agent YAML (replay surface).
# ---------------------------------------------------------------------------


def _write_a2a_yaml(tmp: Path, name: str) -> Path:
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": name,
            "labels": {"capabilities": "echo", "role": "assistant", "team": "smoke"},
        },
        "spec": {"a2a": {"handler": "echo", "port": 8888}},
    }
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p
