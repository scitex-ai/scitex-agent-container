"""Native Hermes JSON-RPC delivery tests using an in-memory transport fake."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._hermes_tui_owner import GATEWAY_FILE
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    _select_session,
    active_sessions,
    clear_heartbeat,
    clear_heartbeat_for_session,
    compress_session,
    gateway_detailed_health,
    observe_turn_activity,
    submit_turn,
    submit_visible_turn,
)


def test_detailed_health_is_authenticated_and_returns_readiness_json(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text(
        json.dumps({"port": 43123}), encoding="utf-8"
    )
    (tmp_path / "hermes-api.key").write_text("secret-token-1234", encoding="utf-8")
    seen = []

    def open_(request, timeout):
        seen.append((request, timeout))
        return nullcontext(
            SimpleNamespace(status=200, read=lambda: b'{"sessions":[],"total":0}')
        )

    # Act
    payload = gateway_detailed_health(tmp_path, urlopen_fn=open_)

    # Assert
    assert (
        payload["status"],
        seen[0][0].full_url,
        seen[0][0].get_header("Authorization"),
    ) == (
        "ok",
        "http://127.0.0.1:43123/api/sessions?limit=1",
        "Bearer secret-token-1234",
    )


def test_detailed_health_rejects_http_200_without_session_store_shape(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text(
        json.dumps({"port": 43123}), encoding="utf-8"
    )
    (tmp_path / "hermes-api.key").write_text("secret-token-1234", encoding="utf-8")

    def open_(_request, timeout):
        del timeout
        return nullcontext(
            SimpleNamespace(
                status=200,
                read=lambda: b'{"detail":"storage unavailable"}',
            )
        )

    # Act
    try:
        gateway_detailed_health(tmp_path, urlopen_fn=open_)
    except HermesTuiRpcError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        observed = str(exc)
    else:
        observed = ""

    # Assert
    assert "malformed response" in observed


class _Socket:
    def __init__(self, status: str | None = None):
        self.sent = []
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        result = {
            "session.active_list": {
                "sessions": [
                    {
                        "id": "live-1",
                        "title": "sac:hub",
                        **({"status": self.status} if self.status else {}),
                    }
                ]
            },
            "session.activate": {"id": "live-1"},
            "prompt.submit": {"status": "steered"},
            "session.steer": {"status": "queued", "text": "act now"},
        }[method]
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _VisibleSocket:
    def __init__(
        self,
        *,
        submit_status: str,
        projections: list[dict],
        session_status: str = "idle",
    ):
        self.sent = []
        self.submit_status = submit_status
        self.projections = iter(projections)
        self.session_status = session_status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.active_list":
            result = {
                "sessions": [
                    {
                        "id": "live-1",
                        "title": "sac:hub",
                        "status": self.session_status,
                    }
                ]
            }
        elif method == "session.activate":
            result = next(self.projections)
            result.setdefault("session_key", "stored-1")
        elif method == "prompt.submit":
            result = {"status": self.submit_status}
        elif method == "session.steer":
            result = {"status": "queued", "text": request["params"]["text"]}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _SearchResponse:
    def __init__(self, payload: dict):
        self.encoded = json.dumps(payload).encode()
        self.read_sizes = []
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size):
        self.read_sizes.append(size)
        return self.encoded


def _search(payload=None):
    response = _SearchResponse(payload or {"results": []})

    def open_search(request, **kwargs):
        response.requests.append((request, kwargs))
        return response

    return response, open_search


class _ControlSocket(_Socket):
    def __init__(self, heartbeat):
        super().__init__()
        self.heartbeat = heartbeat

    def recv(self):
        request = self.sent[-1]
        if request["method"] == "session.active_list":
            result = {"sessions": [{"id": "live-1", "title": "sac:hub"}]}
        elif request["method"] == "session.control":
            result = {"control": {"heartbeat": self.heartbeat}}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(request["method"])
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _CompressionSocket(_Socket):
    def __init__(self, *, status="idle", result=None):
        super().__init__(status=status)
        self.result = result or {
            "status": "compressed",
            "before_tokens": 663_402,
            "after_tokens": 91_000,
            "before_messages": 1324,
            "after_messages": 87,
            "info": {
                "usage": {
                    "context_used": 91_000,
                    "context_max": 1_000_000,
                    "context_source": "provider_usage",
                    "compressions": 1,
                }
            },
        }

    def recv(self):
        request = self.sent[-1]
        if request["method"] == "session.compress":
            result = self.result
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            )
        return super().recv()


def test_compress_session_uses_native_rpc_and_returns_telemetry(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket()
    # Act
    receipt = compress_session(
        tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
    )
    # Assert
    assert (
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
        receipt,
    ) == (
        ["session.active_list", "session.compress"],
        {"session_id": "live-1"},
        type(receipt)(
            session_id="live-1",
            before_tokens=663_402,
            after_tokens=91_000,
            before_messages=1324,
            after_messages=87,
            context_used=91_000,
            context_max=1_000_000,
            context_source="provider_usage",
            compressions=1,
        ),
    )


def test_compress_session_refuses_busy_session_before_mutation(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket(status="working")
    error = None
    # Act
    try:
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)
    except HermesTuiRpcError as exc:  # stx-allow: test-capture (reason: assert error and absence of mutating RPC together.)
        error = exc

    # Assert
    assert (
        "busy" in str(error),
        [request["method"] for request in socket.sent],
    ) == (True, ["session.active_list"])


@pytest.mark.parametrize(
    "result",
    [
        {"status": "aborted"},
        {
            "status": "compressed",
            "before_tokens": 100,
            "after_tokens": 100,
            "before_messages": 10,
            "after_messages": 5,
        },
    ],
)
def test_compress_session_fails_closed_without_proven_reduction(tmp_path, result):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket(result=result)
    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match="did not commit|no strict"):
        action()


@pytest.mark.parametrize(
    ("usage_update", "message"),
    [
        ({"context_used": 1_000_001}, "incomplete context telemetry"),
        ({"context_max": 0}, "incomplete context telemetry"),
        ({"compressions": 0}, "incomplete context telemetry"),
        ({"context_source": ""}, "incomplete context telemetry"),
        ({"context_used": None}, "invalid context_used"),
        ({"compressions": None}, "invalid compressions"),
    ],
)
def test_compress_session_fails_closed_on_invalid_usage_telemetry(
    tmp_path, usage_update, message
):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket()
    socket.result["info"]["usage"].update(usage_update)

    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match=message):
        action()


def test_compress_session_fails_closed_on_missing_usage_telemetry(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    result = {
        "status": "compressed",
        "before_tokens": 100,
        "after_tokens": 50,
        "before_messages": 10,
        "after_messages": 5,
    }
    socket = _CompressionSocket(result=result)

    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match="no post-compression usage"):
        action()


def test_clear_heartbeat_uses_control_plane_without_model_turn(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket(None)

    # Act
    status = clear_heartbeat_for_session(
        tmp_path, "live-1", connect_fn=lambda *args, **kwargs: socket
    )

    # Assert
    assert (
        status,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "absent",
        ["session.control"],
        {"session_id": "live-1", "action": "heartbeat.clear"},
    )


def test_clear_heartbeat_refuses_persisted_state(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket({"status": "active"})

    # Act
    def action():
        clear_heartbeat_for_session(
            tmp_path, "live-1", connect_fn=lambda *a, **k: socket
        )

    # Assert
    with pytest.raises(HermesTuiRpcError, match="did not clear"):
        action()


def test_clear_heartbeat_resolves_named_session_without_model_turn(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket(None)

    # Act
    status = clear_heartbeat(
        tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
    )

    # Assert
    assert (
        status,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "absent",
        ["session.active_list", "session.control"],
        {"session_id": "live-1", "action": "heartbeat.clear"},
    )


def test_submit_turn_targets_same_live_session_and_accepts_steer(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket(status="working")

    # Act
    receipt = submit_turn(
        tmp_path, "hub", "act now", connect_fn=lambda *a, **k: socket
    )

    # Assert
    assert (
        receipt.status,
        receipt.delivery_mode,
        [row["method"] for row in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "steered",
        "steer",
        ["session.active_list", "session.steer"],
        {"session_id": "live-1", "text": "act now"},
    )


def test_explicit_queue_uses_hermes_next_turn_queue_not_active_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class QueueSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "prompt.submit":
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"status": "queued"},
                    }
                )
            return super().recv()

    socket = QueueSocket(status="working")

    # Act
    receipt = submit_turn(
        tmp_path,
        "hub",
        "run this afterward",
        delivery_mode="queue",
        connect_fn=lambda *a, **k: socket,
    )

    # Assert
    assert (
        receipt.delivery_mode,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "queue",
        ["session.active_list", "prompt.submit"],
        {
            "session_id": "live-1",
            "text": "run this afterward",
            "queued": True,
        },
    )


def test_active_default_never_accepts_hermes_next_turn_queue_as_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class RejectingSteerSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "session.steer":
                result = {"status": "rejected", "text": request["params"]["text"]}
                return json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                )
            return super().recv()

    socket = RejectingSteerSocket(status="working")

    error = None
    # Act
    try:
        submit_turn(
            tmp_path, "hub", "urgent correction", connect_fn=lambda *a, **k: socket
        )
    except HermesTuiRpcError as exc:  # stx-allow: test-capture (reason: STX-TQ002 requires Act and Assert to remain separate.)
        error = exc
    # Assert
    assert (
        "did not accept the active-turn steer" in str(error),
        [request["method"] for request in socket.sent],
    ) == (
        True,
        ["session.active_list", "session.steer"],
    )

def test_session_selection_refuses_ambiguous_gateway():
    # Arrange
    rows = [{"id": "one", "title": "other"}, {"id": "two", "title": "another"}]

    # Act
    def action() -> None:
        _select_session(rows, "sac:hub")

    # Assert
    with pytest.raises(HermesTuiRpcError, match="cannot identify one"):
        action()


def test_active_sessions_is_observation_only(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket()

    # Act
    rows = active_sessions(tmp_path, connect_fn=lambda *a, **k: socket)

    # Assert
    assert (rows, [row["method"] for row in socket.sent]) == (
        [{"id": "live-1", "title": "sac:hub"}],
        ["session.active_list"],
    )


@pytest.mark.parametrize(
    ("native_status", "expected_state"),
    [
        ("idle", "idle"),
        ("working", "active"),
        ("waiting", "active"),
        ("starting", "active"),
    ],
)
def test_turn_activity_uses_native_live_session_status(
    tmp_path, native_status, expected_state
):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket(status=native_status)

    # Act
    observed = observe_turn_activity(tmp_path, "hub", connect_fn=lambda *a, **k: socket)

    # Assert
    assert (
        observed.state,
        observed.session_status,
        observed.session_id,
        [row["method"] for row in socket.sent],
    ) == (expected_state, native_status, "live-1", ["session.active_list"])


def test_turn_activity_refuses_unknown_native_status(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")

    def action():
        observe_turn_activity(
            tmp_path, "hub", connect_fn=lambda *a, **k: _Socket(status="mystery")
        )

    # Act
    run = action
    # Assert
    with pytest.raises(HermesTuiRpcError, match="unknown activity status"):
        run()


def _gateway_files(tmp_path):
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")


def test_visible_idle_turn_is_proven_in_native_inflight_projection(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "idle delivery <!-- delivery:n_idle -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": []}, {"inflight": {"user": text}}],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_idle",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent],
    ) == (
        "streaming",
        "session.inflight.user",
        [
            "session.active_list",
            "session.activate",
            "prompt.submit",
            "session.activate",
        ],
    )


def test_visible_busy_turn_is_proven_as_native_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "busy delivery <!-- delivery:n_busy -->"
    socket = _VisibleSocket(
        submit_status="steered",
        session_status="working",
        projections=[
            {"inflight": {"user": "original", "corrections": []}},
            {"inflight": {"user": "original", "corrections": [text]}},
        ],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_busy",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (receipt.status, receipt.visibility) == (
        "steered",
        "session.inflight.corrections",
    )


def test_visible_busy_fallback_is_proven_in_native_queue(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "queued delivery <!-- delivery:n_queued -->"
    socket = _VisibleSocket(
        submit_status="queued",
        session_status="working",
        projections=[{}, {"queued": {"user": text}}],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_queued",
        delivery_mode="queue",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (receipt.status, receipt.visibility) == (
        "queued",
        "session.queued.user",
    )


def test_visible_retry_reuses_transcript_proof_without_duplicate_submit(tmp_path):
    # Arrange: native acceptance succeeded long ago, but the downstream Cards
    # ACK did not. The 7,442-message transcript must never cross the websocket.
    _gateway_files(tmp_path)
    text = "retry delivery <!-- delivery:n_retry -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"message_count": 7_442, "messages": []}],
    )
    response, search = _search(
        {
            "results": [
                {
                    "role": "user",
                    "session_id": "stored-1",
                    "lineage_root": "stored-1",
                    "snippet": "retry delivery <!-- >>>delivery:n_retry<<< -->",
                    "message_count": 7_442,
                }
            ]
        }
    )

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_retry",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    request, request_kwargs = response.requests[0]
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"]["omit_messages"],
        response.read_sizes,
        request.full_url.endswith(
            "/api/sessions/search?q=%22delivery%20n_retry%22&limit=20"
        ),
        request.get_header("X-hermes-session-token"),
        request_kwargs,
    ) == (
        "already_visible",
        "session.search",
        ["session.active_list", "session.activate"],
        True,
        [256 * 1024 + 1],
        True,
        "a-secure-test-token",
        {"timeout": 10.0},
    )


def test_accepted_submit_without_native_visibility_fails_closed(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "unproven delivery <!-- delivery:n_unproven -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": []}, {"messages": []}, {"messages": []}],
    )
    _response, search = _search()

    # Act
    def action():
        submit_visible_turn(
            tmp_path,
            "hub",
            text,
            delivery_id="n_unproven",
            max_observations=2,
            poll_s=0,
            connect_fn=lambda *a, **k: socket,
            urlopen_fn=search,
        )

    # Assert
    with pytest.raises(HermesTuiRpcError, match="accepted.*not visible"):
        action()


def test_accepted_submit_closes_with_post_submit_persisted_identity(tmp_path):
    # Arrange — both lightweight projections miss a just-accepted input, but
    # Hermes' final indexed view has committed its durable delivery marker.
    # Returning failure here would make CCT activate its native fallback rail.
    _gateway_files(tmp_path)
    text = "late delivery <!-- delivery:n_late -->"
    socket = _VisibleSocket(
        submit_status="steered",
        projections=[{"messages": []}, {"messages": []}, {"messages": []}],
    )
    searches = iter(
        [
            {"results": []},
            {
                "results": [
                    {
                        "role": "user",
                        "session_id": "stored-1",
                        "lineage_root": "stored-1",
                        "snippet": "late delivery <!-- >>>delivery:n_late<<< -->",
                    }
                ]
            },
        ]
    )

    def search(_request, **_kwargs):
        return _SearchResponse(next(searches))

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_late",
        max_observations=2,
        poll_s=0,
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent].count("prompt.submit"),
    ) == ("steered", "session.search", 1)
