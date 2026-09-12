"""Native Hermes JSON-RPC delivery tests using an in-memory transport fake."""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.runtimes._hermes_tui_owner import GATEWAY_FILE
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    _select_session,
    submit_turn,
)


class _Socket:
    def __init__(self):
        self.sent = []

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
            "session.active_list": {"sessions": [{"id": "live-1", "title": "sac:hub"}]},
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

    # Act / Assert
    with pytest.raises(HermesTuiRpcError, match="cannot identify one"):
        _select_session(rows, "sac:hub")
