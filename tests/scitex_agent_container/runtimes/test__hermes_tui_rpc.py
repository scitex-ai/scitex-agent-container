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
