"""Native Hermes JSON-RPC delivery tests using an in-memory transport fake."""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.runtimes._hermes_tui_owner import GATEWAY_FILE
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    _select_session,
    active_sessions,
    observe_turn_activity,
    submit_turn,
    submit_visible_turn,
)


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
        }[method]
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _VisibleSocket:
    def __init__(self, *, submit_status: str, projections: list[dict]):
        self.sent = []
        self.submit_status = submit_status
        self.projections = iter(projections)

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
            result = {"sessions": [{"id": "live-1", "title": "sac:hub"}]}
        elif method == "session.activate":
            result = next(self.projections)
            result.setdefault("session_key", "stored-1")
        elif method == "prompt.submit":
            result = {"status": self.submit_status}
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


def test_submit_turn_targets_same_live_session_and_accepts_steer(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket()

    # Act
    status = submit_turn(tmp_path, "hub", "act now", connect_fn=lambda *a, **k: socket)

    # Assert
    assert (
        status,
        [row["method"] for row in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "steered",
        ["session.active_list", "session.activate", "prompt.submit"],
        {"session_id": "live-1", "text": "act now"},
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
        projections=[{}, {"queued": {"user": text}}],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_queued",
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
