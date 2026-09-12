"""Native Hermes JSON-RPC delivery tests using an in-memory transport fake."""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.runtimes._hermes_tui_owner import GATEWAY_FILE
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    DEFAULT_MAX_RPC_FRAME_BYTES,
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
        elif method == "prompt.submit":
            result = {"status": self.submit_status}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


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

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_idle",
        connect_fn=lambda *a, **k: socket,
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

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_busy",
        connect_fn=lambda *a, **k: socket,
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

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_queued",
        connect_fn=lambda *a, **k: socket,
    )

    # Assert
    assert (receipt.status, receipt.visibility) == (
        "queued",
        "session.queued.user",
    )


def test_visible_retry_reuses_transcript_proof_without_duplicate_submit(tmp_path):
    # Arrange: native acceptance succeeded, but the downstream Cards ACK did not.
    _gateway_files(tmp_path)
    text = "retry delivery <!-- delivery:n_retry -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": [{"role": "user", "text": text}]}],
    )

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_retry",
        connect_fn=lambda *a, **k: socket,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent],
    ) == (
        "already_visible",
        "session.messages",
        ["session.active_list", "session.activate"],
    )


def test_visible_marker_handles_transcript_frame_larger_than_one_mib(tmp_path):
    # Arrange — mirrors a long-lived Hermes session whose activate response is
    # larger than websockets' default 1 MiB frame ceiling.
    _gateway_files(tmp_path)
    text = "delivery <!-- delivery:n_large -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": [{"role": "user", "text": "x" * 1_100_000 + text}]}],
    )
    observed: dict = {}

    def connect(*_args, **kwargs):
        observed.update(kwargs)
        return socket

    # Act
    receipt = submit_visible_turn(
        tmp_path, "hub", text, delivery_id="n_large", connect_fn=connect
    )

    # Assert
    assert (
        receipt.status,
        observed["max_size"],
        observed["max_size"] == DEFAULT_MAX_RPC_FRAME_BYTES,
    ) == ("already_visible", 64 * 1024 * 1024, True)


def test_accepted_submit_without_native_visibility_fails_closed(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "unproven delivery <!-- delivery:n_unproven -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": []}, {"messages": []}, {"messages": []}],
    )

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
        )

    # Assert
    with pytest.raises(HermesTuiRpcError, match="accepted.*not visible"):
        action()
