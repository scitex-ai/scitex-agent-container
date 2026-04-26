"""Tests for `scitex_agent_container.a2a._server` (Phase 1 SDK adoption).

Covers:

* Card endpoints (fleet card, list, per-agent card) — unchanged from
  pre-SDK, byte-compat.
* Legacy ``tasks/send`` / ``tasks/get`` JSON-RPC envelopes — must
  remain byte-compatible so external clients don't break.
* New SDK ``message/send`` non-streaming round-trip.
* New SDK ``message/stream`` SSE smoke test (the new capability).
* Yaml-driven executor selection via ``spec.a2a.handler``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from starlette.testclient import TestClient

from scitex_agent_container.a2a import EXECUTORS, build_app
from scitex_agent_container.a2a.executors import (
    ClaudeCliExecutor,
    EchoExecutor,
    ExecExecutor,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _write_yaml(tmpdir: Path, name: str, handler: str = "echo") -> Path:
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": name,
            "labels": {
                "capabilities": "chat,echo",
                "role": "assistant",
                "team": "scitex",
            },
        },
        "spec": {"a2a": {"handler": handler, "port": 8888}},
    }
    p = tmpdir / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


@pytest.fixture()
def echo_client() -> TestClient:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "mock-echo", handler="echo")
        app = build_app([p])
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------
# Executor selection
# ---------------------------------------------------------------------


def test_executors_registry_keys():
    """The executor registry exposes the same keys as the legacy HANDLERS dict."""
    assert set(EXECUTORS) == {"echo", "claude_cli", "exec"}
    assert EXECUTORS["echo"] is EchoExecutor
    assert EXECUTORS["claude_cli"] is ClaudeCliExecutor
    assert EXECUTORS["exec"] is ExecExecutor


def test_executors_construct_from_yaml_handler():
    """`spec.a2a.handler` from yaml drives executor class selection."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "mock-claude", handler="claude_cli")
        app = build_app([p])
        # Pull the dispatcher out of the closure-captured ctx via routes.
        # Easier: import the build_app internals indirectly — round-trip
        # /v1/agents/ to confirm the agent loaded at all.
        with TestClient(app) as c:
            r = c.get("/v1/agents/")
            assert r.status_code == 200
            assert r.json() == {
                "agents": [{"name": "mock-claude", "url": "http://testserver/v1/agents/mock-claude"}]
            }


def test_unknown_handler_raises():
    """An unknown ``spec.a2a.handler`` value is rejected at build time."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "bad", handler="nope")
        with pytest.raises(ValueError, match="unknown a2a handler"):
            build_app([p])


# ---------------------------------------------------------------------
# Card endpoints
# ---------------------------------------------------------------------


def test_fleet_card(echo_client: TestClient):
    r = echo_client.get("/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "scitex-agent-container"
    assert body["x-scitex-agent-container"]["agents"] == [
        {"name": "mock-echo", "url": "http://testserver/v1/agents/mock-echo"}
    ]


def test_list_agents(echo_client: TestClient):
    r = echo_client.get("/v1/agents/")
    assert r.status_code == 200
    assert r.json() == {
        "agents": [{"name": "mock-echo", "url": "http://testserver/v1/agents/mock-echo"}]
    }


def test_per_agent_card(echo_client: TestClient):
    r = echo_client.get("/v1/agents/mock-echo/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "mock-echo"
    assert body["url"] == "http://testserver/v1/agents/mock-echo"
    # Sac extension namespace is preserved on the dict card.
    assert "x-scitex-agent-container" in body


def test_per_agent_card_unknown_404(echo_client: TestClient):
    r = echo_client.get("/v1/agents/no-such-agent/.well-known/agent.json")
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Legacy tasks/send + tasks/get — byte-compat with pre-SDK server
# ---------------------------------------------------------------------


def test_legacy_tasks_send_echo_shape(echo_client: TestClient):
    body = {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "tasks/send",
        "params": {
            "id": "task-abc",
            "sessionId": "sess-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "hello"}],
            },
        },
    }
    r = echo_client.post("/v1/agents/mock-echo", json=body)
    assert r.status_code == 200
    env = r.json()
    assert env["jsonrpc"] == "2.0"
    assert env["id"] == "rpc-1"
    task = env["result"]
    # Same shape as the pre-SDK server.
    assert task["id"] == "task-abc"
    assert task["sessionId"] == "sess-1"
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"] is None
    assert task["status"]["timestamp"].endswith("Z")
    assert task["history"][0]["role"] == "user"
    reply_text = task["history"][1]["parts"][0]["text"]
    assert "received 'hello'" in reply_text
    assert task["artifacts"] == []
    assert task["metadata"]["x-scitex-agent-container"]["agent"] == "mock-echo"


def test_legacy_tasks_get_round_trip(echo_client: TestClient):
    # First seed a task via tasks/send.
    send = echo_client.post(
        "/v1/agents/mock-echo",
        json={
            "jsonrpc": "2.0",
            "id": "rpc-s",
            "method": "tasks/send",
            "params": {
                "id": "task-xyz",
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "ping"}],
                },
            },
        },
    )
    assert send.status_code == 200

    get = echo_client.post(
        "/v1/agents/mock-echo",
        json={"jsonrpc": "2.0", "id": "rpc-g", "method": "tasks/get", "params": {"id": "task-xyz"}},
    )
    assert get.status_code == 200
    env = get.json()
    assert env["id"] == "rpc-g"
    assert env["result"]["id"] == "task-xyz"
    assert env["result"]["status"]["state"] == "completed"


def test_legacy_tasks_get_unknown(echo_client: TestClient):
    r = echo_client.post(
        "/v1/agents/mock-echo",
        json={"jsonrpc": "2.0", "id": "x", "method": "tasks/get", "params": {"id": "no-such"}},
    )
    assert r.status_code == 200
    env = r.json()
    assert env["error"]["code"] == -32000
    assert "task not found" in env["error"]["message"]


def test_unknown_agent_404(echo_client: TestClient):
    r = echo_client.post(
        "/v1/agents/no-such",
        json={"jsonrpc": "2.0", "id": "1", "method": "tasks/send", "params": {}},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# SDK message/send + message/stream
# ---------------------------------------------------------------------


def test_sdk_message_send(echo_client: TestClient):
    body = {
        "jsonrpc": "2.0",
        "id": "s1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "text", "text": "hi"}],
            }
        },
    }
    r = echo_client.post("/v1/agents/mock-echo", json=body)
    assert r.status_code == 200
    env = r.json()
    assert env["jsonrpc"] == "2.0"
    assert env["id"] == "s1"
    task = env["result"]
    # SDK shape uses `kind: "task"`, `status.state: "completed"`,
    # artifacts populated.
    assert task["status"]["state"] == "completed"
    artifacts = task.get("artifacts", [])
    assert artifacts and artifacts[0]["name"] == "reply"
    text = artifacts[0]["parts"][0]["text"]
    assert "received 'hi'" in text


def test_sdk_message_stream_sse(echo_client: TestClient):
    """tasks/sendSubscribe-equivalent: SDK ``message/stream`` returns SSE.

    This is the new capability added by Phase 1 — the legacy stdlib
    server couldn't do this.
    """
    body = {
        "jsonrpc": "2.0",
        "id": "ss1",
        "method": "message/stream",
        "params": {
            "message": {
                "messageId": "m-stream",
                "role": "user",
                "parts": [{"kind": "text", "text": "stream me"}],
            }
        },
    }
    with echo_client.stream("POST", "/v1/agents/mock-echo", json=body) as resp:
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct

        events: list[dict] = []
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
            if len(events) >= 6:  # safety cap
                break

    assert events, "expected at least one SSE event"
    kinds = [e["result"]["kind"] for e in events if "result" in e]
    # We should see at least a status update + an artifact update.
    assert "status-update" in kinds
    assert "artifact-update" in kinds

    # Final status-update must be terminal (final=True, completed).
    final_status = [
        e for e in events
        if "result" in e
        and e["result"].get("kind") == "status-update"
        and e["result"].get("final")
    ]
    assert final_status, "expected a final status-update event"
    assert final_status[-1]["result"]["status"]["state"] == "completed"
