"""Tests for ``_listen._nodes`` — external-node registry + AgentCard
synthesis, exercised end-to-end through the ``sac listen`` inbox routes.

Mirrors ``src/scitex_agent_container/_listen/_nodes.py``. The route
handlers in ``_listen/server.py`` (``node_message_send``,
``node_inbox_stream``) use ``NodeRegistry`` + ``Broker`` from this
module; testing them together is what proves the
``NodeRegistry.register`` / ``card`` / ``synthesize_external_card``
contract.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-3 "External nodes join
the comms graph"): sac must support a node with an identity + inbox
but **no spec and no lifecycle**. The inbox endpoints
(``message:send`` and ``inbox/stream``) live on the always-on
``sac listen`` host control-plane and are keyed by node identity —
not by the presence of an agent YAML.

These tests drive the real Starlette app through
``starlette.testclient.TestClient`` and a real ``uvicorn`` loopback
(no mocks, per handoff §0 Hard rules). Real bearer auth, real
Broker, real SSE — exactly the shape ``sac mcp channel`` will see
in production.

Acceptance from §4: "a plain ``claude`` CLI session running
``sac mcp channel --name <id>`` receives, as
``<channel source="sac" ...>`` blocks, messages a permitted node
sends to ``<id>`` — with no container and no spec for ``<id>``."

This file exercises the HTTP-side of that acceptance: an inbox
stream opens, a message is POSTed, the SSE frame is delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db as _state_db
from scitex_agent_container._state.state_db_nodes import (
    mint_node_token,
    record_lineage,
)

TOKEN = "test-token-wi3"


# ---------------------------------------------------------------------------
# Fixtures — share the shape used by tests/.../_listen/test_server.py so the
# external-node tests run under the same isolated tmp_path topology.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    saved_home = os.environ.get("HOME")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_yaml_env = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_db_const = _state_db.DEFAULT_DB_PATH

    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    db_path = tmp_path / "state.db"
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    _state_db.DEFAULT_DB_PATH = db_path
    _state_db.init_schema(db_path)

    # WI-2 ACL: register the WI-3 demo nodes as siblings under a
    # common root so they share a group. Without this the new
    # ACL gate would deny ``permitted-peer → external-bob`` as
    # cross-group (each unattached node is its own singleton).
    mint_node_token(name="permitted-peer", db_path=db_path)
    mint_node_token(name="external-bob", db_path=db_path)
    mint_node_token(name="root", db_path=db_path)
    record_lineage(child="permitted-peer", parent="root", db_path=db_path)
    record_lineage(child="external-bob", parent="root", db_path=db_path)

    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        _state_db.DEFAULT_DB_PATH = saved_db_const
        for key, val in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml_env),
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db_env),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.fixture
def client(isolated_env):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# POST /agents/<external-id>/message:send — must accept an unknown name
# ---------------------------------------------------------------------------


def test_message_send_to_external_node_returns_2xx(client, auth_headers) -> None:
    """An external node has no YAML and is not in the local registry,
    but ``message:send`` must still accept the POST and route it to
    the inbox bus. The implicit-registration semantics (handoff §4)
    means a 404 here is a bug.
    """
    # Arrange
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "hi from a permitted peer"}],
            },
            "metadata": {"from_agent": "permitted-peer"},
        },
    }
    # Act
    resp = client.post(
        "/agents/external-bob/message:send",
        json=payload,
        headers=auth_headers,
    )
    # Assert
    assert resp.status_code < 400, resp.text


def test_message_send_to_external_node_response_carries_msg_id(
    client, auth_headers
) -> None:
    """The published event must include the broker-minted ``msg_id`` so
    the sender can correlate. This is the same envelope shape the
    sac-managed-agent path mints (see ``a2a._inbox_bus.mint_event``).
    """
    # Arrange
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "hi"}],
            },
            "metadata": {"from_agent": "permitted-peer"},
        },
    }
    # Act
    resp = client.post(
        "/agents/external-bob/message:send",
        json=payload,
        headers=auth_headers,
    )
    body = resp.json()
    # Assert — response carries the minted envelope id (callers correlate against this)
    assert isinstance(body.get("msg_id"), str) and len(body["msg_id"]) >= 16


# ---------------------------------------------------------------------------
# GET /agents/<external-id>/inbox/stream — SSE: deliver POSTed event
# ---------------------------------------------------------------------------


async def _sse_roundtrip(
    isolated_env: Path, payload: dict, *, name: str = "external-bob"
) -> dict:
    """End-to-end roundtrip across the real Starlette app.

    Runs a real ``uvicorn`` on a loopback port so the SSE subscriber
    and the publisher are independent HTTP clients — no in-process
    ASGI transport quirks. The ``Broker`` is the real one in
    :mod:`a2a._inbox_bus`; the only seam is the network socket.
    """
    import contextlib as _contextlib
    import socket
    import threading

    import uvicorn

    app = create_app(token=TOKEN)

    # Pick a free loopback port.
    with _contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    # Wait for the server to come up.
    deadline = asyncio.get_event_loop().time() + 5.0
    while not server.started:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("uvicorn did not start in 5s")
        await asyncio.sleep(0.05)

    headers = {"authorization": f"Bearer {TOKEN}"}
    ready = asyncio.Event()

    async def subscribe() -> dict:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0
        ) as ac:
            async with ac.stream(
                "GET", f"/agents/{name}/inbox/stream", headers=headers
            ) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith(":"):
                        ready.set()
                        continue
                    if line.startswith("data:"):
                        return json.loads(line[len("data:") :].lstrip())
        raise AssertionError("SSE closed without delivering an event")

    sub_task = asyncio.create_task(subscribe())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0
        ) as ac:
            resp = await ac.post(
                f"/agents/{name}/message:send",
                json=payload,
                headers=headers,
            )
        assert resp.status_code < 400, resp.text
        return await asyncio.wait_for(sub_task, timeout=5.0)
    finally:
        if not sub_task.done():
            sub_task.cancel()
            with contextlib.suppress(BaseException):
                await sub_task
        server.should_exit = True
        server_thread.join(timeout=5.0)


def _make_payload(text: str = "hello external", **meta: object) -> dict:
    base_meta = {"from_agent": "permitted-peer"}
    base_meta.update(meta)
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": base_meta,
        },
    }


def test_external_inbox_stream_delivers_posted_event(isolated_env) -> None:
    """End-to-end: subscribe to inbox/stream, POST a ``message:send``
    to the same name, observe the SSE frame.

    Real ``Broker``, real SSE, real ASGI transport — no mocks
    (handoff §0 Hard rules).
    """
    # Arrange
    payload = _make_payload(text="hello external", conversation_id="c-wi3")
    # Act
    event = asyncio.run(_sse_roundtrip(isolated_env, payload))
    # Assert
    assert event.get("content") == "hello external"


def test_external_inbox_stream_event_preserves_from_agent(isolated_env) -> None:
    """The publisher's ``from_agent`` metadata reaches the receiver."""
    # Arrange
    payload = _make_payload(text="x")
    # Act
    event = asyncio.run(_sse_roundtrip(isolated_env, payload))
    # Assert
    assert event.get("from_agent") == "permitted-peer"


# ---------------------------------------------------------------------------
# AgentCard synthesis — an external node has no YAML, so sac must
# synthesise a minimal v1 AgentCard at registration (handoff §4).
# ---------------------------------------------------------------------------


def test_external_node_agent_card_returns_200(client, auth_headers) -> None:
    """The AgentCard route must succeed for a registered external node.
    Currently returns 404 because the lookup goes through ``resolve_config``
    which only knows YAML-backed names.
    """
    # Arrange — register the node by reaching the inbox endpoint first.
    name = "external-bob"
    client.post(
        f"/agents/{name}/message:send",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "message_id": "m1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "register me"}],
                },
                "metadata": {"from_agent": "permitted-peer"},
            },
        },
        headers=auth_headers,
    )
    # Act
    resp = client.get(
        f"/agents/{name}/.well-known/agent-card.json",
        headers=auth_headers,
    )
    # Assert
    assert resp.status_code == 200, resp.text


def test_external_node_agent_card_advertises_node_kind_external(
    client, auth_headers
) -> None:
    """The synthesised card carries
    ``x-scitex-agent-container.node_kind == "external"`` so consumers
    (and orochi later) can distinguish a managed agent from an external
    node without parsing YAML.
    """
    # Arrange — implicit registration via message:send.
    name = "external-bob"
    client.post(
        f"/agents/{name}/message:send",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "message_id": "m1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "x"}],
                },
                "metadata": {"from_agent": "permitted-peer"},
            },
        },
        headers=auth_headers,
    )
    # Act
    resp = client.get(
        f"/agents/{name}/.well-known/agent-card.json",
        headers=auth_headers,
    )
    card = resp.json()
    # Assert
    ext = card.get("x-scitex-agent-container") or {}
    assert ext.get("node_kind") == "external"


def test_external_node_agent_card_supportedinterfaces_url_targets_inbox(
    client, auth_headers
) -> None:
    """The card's supportedInterfaces URL is the agent's inbox base —
    that's what an A2A v1 client uses to send messages.
    """
    # Arrange
    name = "external-bob"
    client.post(
        f"/agents/{name}/message:send",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "message_id": "m1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "x"}],
                },
                "metadata": {"from_agent": "permitted-peer"},
            },
        },
        headers=auth_headers,
    )
    # Act
    resp = client.get(
        f"/agents/{name}/.well-known/agent-card.json",
        headers=auth_headers,
    )
    card = resp.json()
    # Assert
    iface = (card.get("supportedInterfaces") or [{}])[0]
    assert iface.get("url", "").endswith(f"/agents/{name}")


# ---------------------------------------------------------------------------
# Hard-rule guard: unauthenticated requests stay denied. ACL gating
# lives under WI-2; bearer auth is the outer perimeter and must still
# fire for the new endpoints.
# ---------------------------------------------------------------------------


def test_message_send_without_bearer_token_is_rejected(client) -> None:
    # Arrange
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "x"}],
            }
        },
    }
    # Act
    resp = client.post("/agents/anything/message:send", json=payload)
    # Assert
    assert resp.status_code == 401


def test_inbox_stream_without_bearer_token_is_rejected(client) -> None:
    # Arrange / Act
    resp = client.get("/agents/anything/inbox/stream")
    # Assert
    assert resp.status_code == 401
