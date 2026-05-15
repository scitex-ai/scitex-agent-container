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

TQ cleanup: every test carries AAA markers (TQ002), descriptive names
(TQ003), and exactly one assertion (TQ007). Multi-assertion cases are
split into per-behaviour tests, parametrized when natural.
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
from scitex_agent_container.a2a.executors._claude_session import (
    ClaudeSessionExecutor,
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


_REGISTRY_EXPECTED = {
    "echo": EchoExecutor,
    "claude_cli": ClaudeCliExecutor,
    "exec": ExecExecutor,
    "claude_session": ClaudeSessionExecutor,
}


def test_executors_registry_has_expected_keys():
    """The executor registry exposes one entry per built-in handler."""
    # Arrange
    expected_keys = set(_REGISTRY_EXPECTED)
    # Act
    actual_keys = set(EXECUTORS)
    # Assert
    assert actual_keys == expected_keys


@pytest.mark.parametrize("handler,cls", sorted(_REGISTRY_EXPECTED.items()))
def test_executors_registry_maps_handler_to_class(handler, cls):
    """`spec.a2a.handler` value maps to the matching executor class."""
    # Arrange
    registry = EXECUTORS
    # Act
    actual = registry[handler]
    # Assert
    assert actual is cls


def test_executors_yaml_handler_drives_app_build_status():
    """A non-default ``spec.a2a.handler`` still produces a healthy /agents/."""
    # Arrange
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "mock-claude", handler="claude_cli")
        app = build_app([p])
        # Act
        with TestClient(app) as c:
            r = c.get("/agents/")
        # Assert
        assert r.status_code == 200


def test_executors_yaml_handler_drives_app_build_body():
    """A non-default ``spec.a2a.handler`` lists the agent under /agents/."""
    # Arrange
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "mock-claude", handler="claude_cli")
        app = build_app([p])
        # Act
        with TestClient(app) as c:
            body = c.get("/agents/").json()
        # Assert
        assert body == {
            "agents": [
                {
                    "name": "mock-claude",
                    "url": "http://testserver/agents/mock-claude",
                }
            ]
        }


def test_unknown_handler_raises_value_error():
    """An unknown ``spec.a2a.handler`` value is rejected at build time."""
    # Arrange
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _write_yaml(tmp, "bad", handler="nope")
        # Act
        ctx = pytest.raises(ValueError, match="unknown a2a handler")
        # Assert
        with ctx:
            build_app([p])


# ---------------------------------------------------------------------
# AgentCard protobuf adapter
# ---------------------------------------------------------------------


@pytest.fixture()
def _proto_card_minimal():
    v3 = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": "mock-echo",
            "labels": {"capabilities": "chat,echo", "role": "assistant"},
        },
        "spec": {"a2a": {"handler": "echo"}},
    }
    return project_card_proto("mock-echo", v3, "http://localhost:8888")


def test_project_card_proto_name_matches_agent(_proto_card_minimal):
    """The proto card's ``name`` field copies the agent name."""
    # Arrange
    proto = _proto_card_minimal
    # Act
    actual = proto.name
    # Assert
    assert actual == "mock-echo"


def test_project_card_proto_capabilities_streaming_true(_proto_card_minimal):
    """The proto card advertises streaming capability."""
    # Arrange
    proto = _proto_card_minimal
    # Act
    actual = proto.capabilities.streaming
    # Assert
    assert actual is True


def test_project_card_proto_emits_single_skill(_proto_card_minimal):
    """The proto card carries exactly one skill (derived from the role)."""
    # Arrange
    proto = _proto_card_minimal
    # Act
    n = len(proto.skills)
    # Assert
    assert n == 1


def test_project_card_proto_skill_id_uses_role(_proto_card_minimal):
    """The single skill's id is ``<name>.<role>``."""
    # Arrange
    proto = _proto_card_minimal
    # Act
    actual = proto.skills[0].id
    # Assert
    assert actual == "mock-echo.assistant"


# ---------------------------------------------------------------------
# Card endpoints
# ---------------------------------------------------------------------


def test_fleet_card_endpoint_returns_200(echo_client: TestClient):
    """The fleet-level agent-card endpoint responds 200 OK."""
    # Arrange
    client = echo_client
    # Act
    r = client.get("/.well-known/agent-card.json")
    # Assert
    assert r.status_code == 200


def test_fleet_card_endpoint_carries_fleet_name(echo_client: TestClient):
    """The fleet card's ``name`` is the sac fleet name."""
    # Arrange
    client = echo_client
    # Act
    name = client.get("/.well-known/agent-card.json").json()["name"]
    # Assert
    assert name == "scitex-agent-container"


def test_fleet_card_endpoint_lists_member_agents(echo_client: TestClient):
    """The fleet card's sac extension lists each member agent."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/.well-known/agent-card.json").json()
    # Assert
    assert body["x-scitex-agent-container"]["agents"] == [
        {"name": "mock-echo", "url": "http://testserver/agents/mock-echo"}
    ]


def test_list_agents_endpoint_returns_200(echo_client: TestClient):
    """The /agents/ index responds 200 OK."""
    # Arrange
    client = echo_client
    # Act
    r = client.get("/agents/")
    # Assert
    assert r.status_code == 200


def test_list_agents_endpoint_enumerates_members(echo_client: TestClient):
    """The /agents/ index body enumerates each agent and its URL."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/agents/").json()
    # Assert
    assert body == {
        "agents": [{"name": "mock-echo", "url": "http://testserver/agents/mock-echo"}]
    }


def test_per_agent_card_returns_200(echo_client: TestClient):
    """The per-agent card endpoint responds 200 OK for a known agent."""
    # Arrange
    client = echo_client
    # Act
    r = client.get("/agents/mock-echo/.well-known/agent-card.json")
    # Assert
    assert r.status_code == 200


def test_per_agent_card_name_is_agent_name(echo_client: TestClient):
    """The per-agent card's ``name`` is the agent's own name."""
    # Arrange
    client = echo_client
    # Act
    name = client.get("/agents/mock-echo/.well-known/agent-card.json").json()["name"]
    # Assert
    assert name == "mock-echo"


def test_per_agent_card_supported_interface_url(echo_client: TestClient):
    """ADR-0004: v1 card publishes URL under supportedInterfaces[0]."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/agents/mock-echo/.well-known/agent-card.json").json()
    # Assert
    assert body["supportedInterfaces"][0]["url"] == "http://testserver/agents/mock-echo"


def test_per_agent_card_supported_interface_binding(echo_client: TestClient):
    """The per-agent card declares HTTP+JSON as its protocol binding."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/agents/mock-echo/.well-known/agent-card.json").json()
    # Assert
    assert body["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"


def test_per_agent_card_preserves_sac_extension(echo_client: TestClient):
    """The sac extension namespace is preserved on the dict card."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/agents/mock-echo/.well-known/agent-card.json").json()
    # Assert
    assert "x-scitex-agent-container" in body


def test_per_agent_card_unknown_returns_404(echo_client: TestClient):
    """An unknown agent name on the card endpoint returns 404."""
    # Arrange
    client = echo_client
    # Act
    r = client.get("/agents/no-such-agent/.well-known/agent-card.json")
    # Assert
    assert r.status_code == 404


def test_unknown_agent_message_send_returns_404(echo_client: TestClient):
    """Posting message/send to an unknown agent returns 404."""
    # Arrange
    client = echo_client
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {},
    }
    # Act
    r = client.post("/agents/no-such/message:send", json=payload)
    # Assert
    assert r.status_code == 404


# ---------------------------------------------------------------------
# SDK message/send + message/stream
# ---------------------------------------------------------------------


def _send_message_body(message_id: str, text: str, *, rpc_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            }
        },
    }


@pytest.fixture()
def _send_response(echo_client: TestClient):
    body = _send_message_body("m1", "hi", rpc_id="s1")
    return echo_client.post(
        "/agents/mock-echo/message:send",
        json=body,
        headers={"A2A-Version": "1.0"},
    )


def test_sdk_send_message_returns_200(_send_response):
    """``SendMessage`` returns HTTP 200."""
    # Arrange
    r = _send_response
    # Act
    code = r.status_code
    # Assert
    assert code == 200


def test_sdk_send_message_envelope_is_jsonrpc_2(_send_response):
    """The response envelope advertises jsonrpc 2.0."""
    # Arrange
    env = _send_response.json()
    # Act
    actual = env["jsonrpc"]
    # Assert
    assert actual == "2.0"


def test_sdk_send_message_envelope_echoes_request_id(_send_response):
    """The response envelope echoes the request's jsonrpc id."""
    # Arrange
    env = _send_response.json()
    # Act
    actual = env["id"]
    # Assert
    assert actual == "s1"


def test_sdk_send_message_envelope_carries_result(_send_response):
    """The response envelope contains a ``result`` member (not ``error``)."""
    # Arrange
    env = _send_response.json()
    # Act
    has_result = "result" in env
    # Assert
    assert has_result, f"expected result, got {env}"


def test_sdk_send_message_task_status_completed(_send_response):
    """The resulting task's state is COMPLETED."""
    # Arrange
    env = _send_response.json()
    task = env["result"].get("task") or env["result"]
    status = task.get("status") or {}
    state = str(status.get("state", "")).upper()
    # Act
    completed = "COMPLETED" in state or state == "COMPLETED"
    # Assert
    assert completed, f"unexpected state: {state!r}"


# --- streaming ---------------------------------------------------------


def _collect_stream_events(client: TestClient) -> list[dict]:
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
    with client.stream(
        "POST",
        "/agents/mock-echo/message:send",
        json=body,
        headers={"A2A-Version": "1.0"},
    ) as resp:
        status_code = resp.status_code
        content_type = resp.headers.get("content-type", "")
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
    return {"status": status_code, "content_type": content_type, "events": events}


@pytest.fixture()
def _stream_result(echo_client: TestClient):
    return _collect_stream_events(echo_client)


def test_sdk_streaming_message_returns_200(_stream_result):
    """``SendStreamingMessage`` returns HTTP 200."""
    # Arrange
    result = _stream_result
    # Act
    code = result["status"]
    # Assert
    assert code == 200


def test_sdk_streaming_message_content_type_sse(_stream_result):
    """``SendStreamingMessage`` advertises text/event-stream."""
    # Arrange
    result = _stream_result
    # Act
    ct = result["content_type"]
    # Assert
    assert "text/event-stream" in ct


def test_sdk_streaming_message_emits_events(_stream_result):
    """The SSE stream yields at least one parseable event."""
    # Arrange
    result = _stream_result
    # Act
    events = result["events"]
    # Assert
    assert events, "expected at least one SSE event"


def test_sdk_streaming_message_final_event_completed(_stream_result):
    """The SSE stream contains a COMPLETED state marker."""
    # Arrange
    result = _stream_result
    flat = json.dumps(result["events"]).upper()
    # Act
    completed = "COMPLETED" in flat or "STATE_COMPLETED" in flat
    # Assert
    assert completed, f"no completed state in events: {result['events']!r}"


# --- get-task round-trip ------------------------------------------------


@pytest.fixture()
def _round_trip(echo_client: TestClient):
    send = echo_client.post(
        "/agents/mock-echo/message:send",
        json=_send_message_body("m-rt", "round-trip", rpc_id="send-1"),
        headers={"A2A-Version": "1.0"},
    )
    send_env = send.json()
    result = send_env["result"]
    task = result.get("task") or result
    task_id = task.get("id") or task.get("task_id")
    get = echo_client.post(
        "/agents/mock-echo/message:send",
        json={
            "jsonrpc": "2.0",
            "id": "get-1",
            "method": "GetTask",
            "params": {"id": task_id},
        },
        headers={"A2A-Version": "1.0"},
    )
    return {
        "send_status": send.status_code,
        "task_id": task_id,
        "get_status": get.status_code,
        "get_env": get.json(),
        "send_result": result,
    }


def test_sdk_round_trip_send_returns_200(_round_trip):
    """The initial ``SendMessage`` returns 200."""
    # Arrange
    rt = _round_trip
    # Act
    code = rt["send_status"]
    # Assert
    assert code == 200


def test_sdk_round_trip_send_yields_task_id(_round_trip):
    """``SendMessage`` populates a task id reachable from the result."""
    # Arrange
    rt = _round_trip
    # Act
    task_id = rt["task_id"]
    # Assert
    assert task_id, f"could not locate task id in send result: {rt['send_result']}"


def test_sdk_round_trip_get_task_returns_200(_round_trip):
    """``GetTask`` for the returned task id responds 200."""
    # Arrange
    rt = _round_trip
    # Act
    code = rt["get_status"]
    # Assert
    assert code == 200


def test_sdk_round_trip_get_task_echoes_request_id(_round_trip):
    """``GetTask``'s response echoes the jsonrpc request id."""
    # Arrange
    env = _round_trip["get_env"]
    # Act
    actual = env["id"]
    # Assert
    assert actual == "get-1"


def test_sdk_round_trip_get_task_envelope_carries_result(_round_trip):
    """``GetTask``'s response carries a ``result`` member (not ``error``)."""
    # Arrange
    env = _round_trip["get_env"]
    # Act
    has_result = "result" in env
    # Assert
    assert has_result, f"expected result, got {env}"
