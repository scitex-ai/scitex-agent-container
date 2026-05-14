"""Tests for `scitex_agent_container.a2a._server` (pure a2a-sdk surface).

Covers:

* Card endpoints (fleet card, list, per-agent card) — sac dict shape.
* AgentCard protobuf adapter — the SDK's ``DefaultRequestHandler``
  accepts the proto card sac builds from its v3 YAML.
* SDK ``message/send`` non-streaming round-trip.
* SDK ``message/stream`` SSE round-trip (the new capability).
* Yaml-driven executor selection via ``spec.a2a.handler``.

There is **no** legacy ``tasks/send`` / ``tasks/get`` byte-compat path
— sac speaks current A2A only.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container.a2a import (
    EXECUTORS,
    build_app,
    project_card_proto,
)
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
    """The executor registry exposes one entry per built-in handler."""
    assert set(EXECUTORS) == {"echo", "claude_session", "claude_cli", "exec"}
    assert EXECUTORS["echo"] is EchoExecutor
    assert EXECUTORS["claude_cli"] is ClaudeCliExecutor
    assert EXECUTORS["exec"] is ExecExecutor
    # claude_session — SDK-backed (recommended for new agents)
    from scitex_agent_container.a2a.executors._claude_session import (
        ClaudeSessionExecutor,
    )

    assert EXECUTORS["claude_session"] is ClaudeSessionExecutor


def test_executors_construct_from_yaml_handler():
    """`spec.a2a.handler` from yaml drives executor class selection."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "mock-claude", handler="claude_cli")
        app = build_app([p])
        with TestClient(app) as c:
            r = c.get("/v1/sac/agents/")
            assert r.status_code == 200
            assert r.json() == {
                "agents": [
                    {
                        "name": "mock-claude",
                        "url": "http://testserver/v1/sac/agents/mock-claude",
                    }
                ]
            }


def test_unknown_handler_raises():
    """An unknown ``spec.a2a.handler`` value is rejected at build time."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "bad", handler="nope")
        with pytest.raises(ValueError, match="unknown a2a handler"):
            build_app([p])


# ---------------------------------------------------------------------
# AgentCard protobuf adapter
# ---------------------------------------------------------------------


def test_project_card_proto_minimal():
    """The adapter strips sac extensions and produces a valid proto card."""
    v3 = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": "mock-echo",
            "labels": {"capabilities": "chat,echo", "role": "assistant"},
        },
        "spec": {"a2a": {"handler": "echo"}},
    }
    proto = project_card_proto("mock-echo", v3, "http://localhost:8888")
    assert proto.name == "mock-echo"
    assert proto.capabilities.streaming is True
    assert len(proto.skills) == 1
    assert proto.skills[0].id == "mock-echo.assistant"


# ---------------------------------------------------------------------
# Card endpoints
# ---------------------------------------------------------------------


def test_fleet_card(echo_client: TestClient):
    r = echo_client.get("/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "scitex-agent-container"
    assert body["x-scitex-agent-container"]["agents"] == [
        {"name": "mock-echo", "url": "http://testserver/v1/sac/agents/mock-echo"}
    ]


def test_list_agents(echo_client: TestClient):
    r = echo_client.get("/v1/sac/agents/")
    assert r.status_code == 200
    assert r.json() == {
        "agents": [
            {"name": "mock-echo", "url": "http://testserver/v1/sac/agents/mock-echo"}
        ]
    }


def test_per_agent_card(echo_client: TestClient):
    r = echo_client.get("/v1/sac/agents/mock-echo/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "mock-echo"
    assert body["url"] == "http://testserver/v1/sac/agents/mock-echo"
    # Sac extension namespace is preserved on the dict card.
    assert "x-scitex-agent-container" in body


def test_per_agent_card_unknown_404(echo_client: TestClient):
    r = echo_client.get("/v1/sac/agents/no-such-agent/.well-known/agent.json")
    assert r.status_code == 404


def test_unknown_agent_404(echo_client: TestClient):
    r = echo_client.post(
        "/v1/sac/agents/no-such",
        json={"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# SDK message/send + message/stream
# ---------------------------------------------------------------------


def test_sdk_send_message(echo_client: TestClient):
    """Pure SDK uses gRPC-style method names: ``SendMessage``."""
    body = {
        "jsonrpc": "2.0",
        "id": "s1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "hi"}],
            }
        },
    }
    r = echo_client.post(
        "/v1/sac/agents/mock-echo", json=body, headers={"A2A-Version": "1.0"}
    )
    assert r.status_code == 200
    env = r.json()
    assert env["jsonrpc"] == "2.0"
    assert env["id"] == "s1"
    assert "result" in env, f"expected result, got {env}"
    task = env["result"].get("task") or env["result"]
    status = task.get("status") or {}
    state = status.get("state", "")
    assert "COMPLETED" in str(state).upper() or state == "completed"


def test_sdk_send_streaming_message_sse(echo_client: TestClient):
    """Pure SDK uses ``SendStreamingMessage`` and returns an SSE stream."""
    body = {
        "jsonrpc": "2.0",
        "id": "ss1",
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "message_id": "m-stream",
                "role": "ROLE_USER",
                "parts": [{"text": "stream me"}],
            }
        },
    }
    with echo_client.stream(
        "POST",
        "/v1/sac/agents/mock-echo",
        json=body,
        headers={"A2A-Version": "1.0"},
    ) as resp:
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
    # Final event must indicate task completion.
    flat = json.dumps(events).upper()
    assert "COMPLETED" in flat or "STATE_COMPLETED" in flat


def test_sdk_get_task_round_trip(echo_client: TestClient):
    """After ``SendMessage``, the resulting task is fetchable via ``GetTask``."""
    send = echo_client.post(
        "/v1/sac/agents/mock-echo",
        json={
            "jsonrpc": "2.0",
            "id": "send-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "message_id": "m-rt",
                    "role": "ROLE_USER",
                    "parts": [{"text": "round-trip"}],
                }
            },
        },
        headers={"A2A-Version": "1.0"},
    )
    assert send.status_code == 200
    result = send.json()["result"]
    task = result.get("task") or result
    task_id = task.get("id") or task.get("task_id")
    assert task_id, f"could not locate task id in send result: {result}"

    get = echo_client.post(
        "/v1/sac/agents/mock-echo",
        json={
            "jsonrpc": "2.0",
            "id": "get-1",
            "method": "GetTask",
            "params": {"id": task_id},
        },
        headers={"A2A-Version": "1.0"},
    )
    assert get.status_code == 200
    env = get.json()
    assert env["id"] == "get-1"
    assert "result" in env, f"expected result, got {env}"
