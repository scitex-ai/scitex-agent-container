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
import os
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
from scitex_agent_container.a2a.executors._openai_session import (
    OpenAISessionExecutor,
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
        "spec": {"a2a": {"handler": handler, "port": 8888}},  # stx-allow: STX-NL001
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
    "openai_session": OpenAISessionExecutor,
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
                    "supportedInterfaces": [
                        {
                            "url": "http://testserver/agents/mock-claude",
                            "protocolBinding": "HTTP+JSON",
                            "tenant": "mock-claude",
                            "protocolVersion": "1.0",
                        }
                    ],
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
    """The fleet card's sac extension lists each member agent (v1 shape)."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/.well-known/agent-card.json").json()
    # Assert
    # Each member mirrors the v1 AgentCard shape: URL under
    # supportedInterfaces[], not a top-level `url` (ADR-0004 D11).
    assert body["x-scitex-agent-container"]["agents"] == [
        {
            "name": "mock-echo",
            "supportedInterfaces": [
                {
                    "url": "http://testserver/agents/mock-echo",
                    "protocolBinding": "HTTP+JSON",
                    "tenant": "mock-echo",
                    "protocolVersion": "1.0",
                }
            ],
        }
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
    """The /agents/ index body enumerates each agent in v1 shape."""
    # Arrange
    client = echo_client
    # Act
    body = client.get("/agents/").json()
    # Assert
    # Each member mirrors the v1 AgentCard shape: URL under
    # supportedInterfaces[], not a top-level `url` (ADR-0004 D11).
    assert body == {
        "agents": [
            {
                "name": "mock-echo",
                "supportedInterfaces": [
                    {
                        "url": "http://testserver/agents/mock-echo",
                        "protocolBinding": "HTTP+JSON",
                        "tenant": "mock-echo",
                        "protocolVersion": "1.0",
                    }
                ],
            }
        ]
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


# ---------------------------------------------------------------------
# WI-1 — channel-bus durability + replay-on-reconnect (handoff §4)
#
# Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-1 "Durability /
# replay-on-reconnect"):
#
#   * An event POSTed to ``message:send`` while no SSE subscriber is
#     connected MUST be delivered on connect.
#   * Kill + reconnect MUST replay exactly the missed events.
#   * Nothing is ever dropped silently.
#
# These tests drive the real Starlette app via a real ``uvicorn`` on a
# loopback port (no mocks, per handoff §0). The ``channel_events``
# table is the durability surface; the SSE handler reads from it on
# connect and stamps the SSE ``id:`` line so a Last-Event-ID reconnect
# resumes at the right cursor.
# ---------------------------------------------------------------------

import asyncio as _asyncio
import contextlib as _contextlib
import socket as _socket

import httpx as _httpx

from scitex_agent_container._state import state_db as _state_db
from tests.scitex_agent_container._helpers.loopback_server import (
    run_loopback as _run_loopback_shared,
)


@pytest.fixture
def _isolated_db(tmp_path: Path):
    """Point state.db at a tmp file for this test.

    PA-306 no-mocks: yield-based fixture saving/restoring real state
    (no ``monkeypatch``). Both the env var and the module-level
    constant are touched so callers that read either path see the
    isolated db.
    """
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = _state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    _state_db.DEFAULT_DB_PATH = db
    _state_db.init_schema(db)
    try:
        yield db
    finally:
        _state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def _send_payload(text: str, *, from_agent: str = "alice") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m-x",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": {"from_agent": from_agent},
        },
    }


@_contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin up uvicorn on a loopback port for a single test block.

    Startup wait lives in the shared helper — the hand-rolled 5s ceiling this
    used to carry raced the app's lifespan startup (measured 7.49s under load)
    and turned the py3.11 leg red. See ``_helpers/loopback_server.py``.
    """
    with _run_loopback_shared(app, port) as p:
        yield p


def _free_port() -> int:
    with _contextlib.closing(_socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _consume_first_event(url: str, *, headers: dict | None = None) -> dict:
    """Open the SSE stream, read until the first ``data:`` frame."""
    async with _httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url, headers=headers or {}) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE stream {url!r} closed without a data frame")


async def _consume_event_with_id(
    url: str, *, headers: dict | None = None
) -> tuple[str | None, dict]:
    """Like ``_consume_first_event`` but also returns the SSE ``id:`` line."""
    seen_id: str | None = None
    async with _httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url, headers=headers or {}) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("id:"):
                    seen_id = line[len("id:") :].strip()
                    continue
                if line.startswith("data:"):
                    return seen_id, json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE stream {url!r} closed without a data frame")


@pytest.fixture
def _durable_publish_row(_isolated_db, tmp_path):
    """POST one ``message:send`` with no subscriber; return the persisted row."""
    # Arrange
    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    with TestClient(app) as client:
        # Act
        resp = client.post(
            "/agents/bob/message:send",
            json=_send_payload("hello durable", from_agent="alice"),
        )
    assert resp.status_code in (200, 201, 202), resp.text
    # Assert handled by per-behaviour tests below.
    with _state_db.open_db(_isolated_db) as conn:
        rows = conn.execute(
            "SELECT id, target, source, content, delivered_at FROM channel_events"
        ).fetchall()
    return rows


def test_publish_with_no_subscriber_persists_exactly_one_row(
    _durable_publish_row,
) -> None:
    """The broker fans out to zero queues, but the event lands once
    in ``channel_events``."""
    # Arrange
    rows = _durable_publish_row
    # Act
    count = len(rows)
    # Assert
    assert count == 1


def test_persisted_row_target_matches_path_param(_durable_publish_row) -> None:
    """``target`` carries the agent name from the URL."""
    # Arrange
    row = _durable_publish_row[0]
    # Act
    actual = row["target"]
    # Assert
    assert actual == "bob"


def test_persisted_row_source_carries_from_agent_metadata(
    _durable_publish_row,
) -> None:
    """``source`` carries the publisher's ``from_agent`` metadata."""
    # Arrange
    row = _durable_publish_row[0]
    # Act
    actual = row["source"]
    # Assert
    assert actual == "alice"


def test_persisted_row_content_carries_message_text(_durable_publish_row) -> None:
    """``content`` carries the joined ``message.parts[*].text``."""
    # Arrange
    row = _durable_publish_row[0]
    # Act
    actual = row["content"]
    # Assert
    assert actual == "hello durable"


def test_persisted_row_delivered_at_is_null_with_no_subscriber(
    _durable_publish_row,
) -> None:
    """``delivered_at`` stays NULL until the first subscriber receives."""
    # Arrange
    row = _durable_publish_row[0]
    # Act
    actual = row["delivered_at"]
    # Assert
    assert actual is None


def test_event_posted_before_subscribe_is_replayed_on_connect(
    _isolated_db, tmp_path: Path
) -> None:
    """Acceptance criterion (handoff §4): "an event POSTed with no
    subscriber is delivered on connect".

    The publisher's POST status is treated as a precondition (raise
    on failure) rather than an assertion so the test carries exactly
    one assert — the SSE-replayed payload (TQ007).
    """
    # Arrange
    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        with _httpx.Client(timeout=5.0) as c:
            r = c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("queued for bob"),
            )
            if r.status_code not in (200, 201, 202):
                raise RuntimeError(
                    f"precondition: publisher POST returned {r.status_code}: {r.text!r}"
                )
        # Act — subscribe and read the replay.
        event = _asyncio.run(
            _consume_first_event(f"http://127.0.0.1:{port}/agents/bob/inbox/stream")
        )
    # Assert
    assert event.get("content") == "queued for bob"


def test_replayed_event_is_marked_delivered_after_first_delivery(
    _isolated_db, tmp_path: Path
) -> None:
    """``delivered_at`` is set the first time the event reaches a live
    subscriber. The replay path stamps it inline so the next reconnect
    does not re-yield the same event from the undelivered window."""
    # Arrange
    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        with _httpx.Client(timeout=5.0) as c:
            c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("queued for bob"),
            )
        _asyncio.run(
            _consume_first_event(f"http://127.0.0.1:{port}/agents/bob/inbox/stream")
        )
    # Act
    with _state_db.open_db(_isolated_db) as conn:
        row = conn.execute(
            "SELECT delivered_at FROM channel_events WHERE target='bob'"
        ).fetchone()
    # Assert
    assert row["delivered_at"] is not None


def test_sse_id_line_is_persisted_row_id(_isolated_db, tmp_path: Path) -> None:
    """The SSE ``id:`` line carries the channel_events row id — the
    cursor a reconnecting client echoes back as Last-Event-ID."""
    # Arrange
    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        with _httpx.Client(timeout=5.0) as c:
            c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("first"),
            )
        sse_id, _event = _asyncio.run(
            _consume_event_with_id(f"http://127.0.0.1:{port}/agents/bob/inbox/stream")
        )
    with _state_db.open_db(_isolated_db) as conn:
        row = conn.execute(
            "SELECT id FROM channel_events WHERE target='bob'"
        ).fetchone()
    # Act
    matches = sse_id is not None and int(sse_id) == int(row["id"])
    # Assert
    assert matches


# ---------------------------------------------------------------------
# Regression: the per-agent A2A send path must propagate metadata.ack
# into the minted event. It previously omitted ``ack=`` (its twin in
# ``_listen/server.py`` included it), so an auto-ack arriving via this
# path was minted with ``ack=False`` — the receiver's loop-guard then
# failed to recognise it and auto-acked back, ping-ponging forever.
# ---------------------------------------------------------------------


def _ack_send_payload(*, from_agent: str = "alice") -> dict:
    """A ``message:send`` body shaped like an auto-ack (``metadata.ack``)."""
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m-ack",
                "role": "ROLE_USER",
                "parts": [{"text": ""}],
            },
            "metadata": {"from_agent": from_agent, "ack": True},
        },
    }


@pytest.fixture
def _persisted_ack_event(_isolated_db, tmp_path):
    """POST an ack-flagged message:send; return the round-tripped event.

    Real end-to-end path (TestClient -> _publish_channel_event ->
    mint_event -> persist_event), no mocks. ``persist_event`` stores the
    minted envelope verbatim in ``meta_json``, so the returned dict is
    exactly what the bus would deliver to a subscriber.
    """
    # Arrange
    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    # Act
    with TestClient(app) as client:
        resp = client.post(
            "/agents/bob/message:send",
            json=_ack_send_payload(from_agent="alice"),
        )
    assert resp.status_code in (200, 201, 202), resp.text
    # Assert handled by per-behaviour tests below.
    with _state_db.open_db(_isolated_db) as conn:
        row = conn.execute(
            "SELECT meta_json FROM channel_events WHERE target='bob'"
        ).fetchone()
    return json.loads(row["meta_json"])


def test_a2a_send_path_propagates_ack_flag_into_event(_persisted_ack_event):
    """The minted event carries top-level ``ack=True`` (was dropped)."""
    # Arrange
    event = _persisted_ack_event
    # Act
    actual = event.get("ack")
    # Assert
    assert actual is True


def test_a2a_published_ack_event_is_not_re_acked(_persisted_ack_event):
    """The loop-guard recognises the round-tripped ack and declines."""
    # Arrange
    from scitex_agent_container._mcp.channel import _should_auto_ack

    event = _persisted_ack_event
    # Act
    should = _should_auto_ack(event)
    # Assert
    assert should is False
