"""Hermes session events are the heartbeat's authoritative turn instrument."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._tui_heartbeat_loop import _beat_one
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container.runtimes._hermes_heartbeat import (
    observe_hermes_heartbeat,
)
from scitex_agent_container.runtimes._hermes_heartbeat_projection import (
    HermesHeartbeatProjectionError,
    read_hermes_heartbeat_projection,
    refresh_hermes_heartbeat_projection,
)


class _Socket:
    def __init__(
        self,
        *,
        epoch: str,
        replays: list[dict],
        statuses: list[str],
        session_id: str = "session-1",
    ):
        self.epoch = epoch
        self.replays = iter(replays)
        self.statuses = iter(statuses)
        self.session_id = session_id
        self.sent: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        request = self.sent[-1]
        if request["method"] == "session.active_list":
            result = {
                "sessions": [
                    {
                        "id": self.session_id,
                        "title": "sac:scholar",
                        "status": next(self.statuses),
                    }
                ]
            }
        elif request["method"] == "session.events.since":
            replay = next(self.replays)
            result = {
                "events": replay["events"],
                "latest_seq": replay["latest_seq"],
                "truncated": replay.get("truncated", False),
                "epoch": self.epoch,
                "open_requests": [],
            }
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(request["method"])
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def _gateway(state_dir, generation: str = "generation-1") -> None:
    (state_dir / "hermes-tui-gateway.json").write_text(
        json.dumps({"generation": generation, "port": 19000}), encoding="utf-8"
    )
    (state_dir / "hermes-api.key").write_text(
        "a-secure-test-token\n", encoding="utf-8"
    )


def _event(seq: int, kind: str, payload: dict | None = None) -> dict:
    return {"seq": seq, "type": kind, "payload": payload or {}}


def _observe(tmp_path, socket: _Socket, previous: dict | None = None):
    return observe_hermes_heartbeat(
        tmp_path,
        "scholar",
        previous=previous,
        connect_fn=lambda *args, **kwargs: socket,
    )


def test_accepted_turn_is_counted_from_message_start(tmp_path):
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[{"events": [_event(1, "message.start")], "latest_seq": 1}],
        statuses=["working", "working"],
    )

    # Act
    observed = _observe(tmp_path, socket)

    # Assert
    assert (observed.state, observed.turns_accepted, observed.turns_completed) == (
        "busy",
        1,
        0,
    )


def test_streamed_tool_execution_is_counted_from_tool_start(tmp_path):
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.delta", {"text": "checking"}),
                    _event(3, "tool.start", {"tool_id": "call-1", "name": "terminal"}),
                ],
                "latest_seq": 3,
            }
        ],
        statuses=["working", "working"],
    )

    # Act
    observed = _observe(tmp_path, socket)

    # Assert
    assert (observed.tools_started, observed.tools_inflight, observed.last_event_type) == (
        1,
        1,
        "tool.start",
    )


def test_completed_turn_is_counted_from_terminal_event(tmp_path):
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "tool.start", {"tool_id": "call-1"}),
                    _event(3, "tool.complete", {"tool_id": "call-1"}),
                    _event(4, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 4,
            }
        ],
        statuses=["working", "idle"],
    )

    # Act
    observed = _observe(tmp_path, socket)

    # Assert
    assert (
        observed.state,
        observed.turns_completed,
        observed.tools_completed,
        observed.tools_inflight,
        observed.last_turn_status,
    ) == ("ready", 1, 1, 0, "complete")


def test_gateway_crash_leaves_the_last_heartbeat_untouched(tmp_path):
    # Arrange
    heartbeat = tmp_path / "heartbeat.json"
    original = b'{"writer":"hermes-session-events","turns_completed":4}'
    heartbeat.write_bytes(original)
    agent = {
        "name": "scholar",
        "state_dir": tmp_path,
        "config": SimpleNamespace(harness="hermes"),
    }

    def crash(*_args, **_kwargs):
        raise RuntimeError("gateway disappeared")

    # Act
    written = _beat_one(
        agent,
        snapshot={"tui-scholar": 1_800_000_000},
        write_fn=lambda *_args, **_kwargs: None,
        hermes_observe_fn=crash,
    )

    # Assert
    assert (written, heartbeat.read_bytes()) == (False, original)


def test_replay_epoch_restart_resets_the_engine_incarnation(tmp_path):
    # Arrange
    _gateway(tmp_path)
    previous = {
        "writer": "hermes-session-events",
        "hermes_gateway_generation": "generation-1",
        "engine_incarnation_id": "generation-1:epoch-old:session-1",
        "hermes_event_epoch": "epoch-old",
        "hermes_event_seq": 91,
        "hermes_session_id": "session-1",
        "turns_accepted": 9,
        "turns_completed": 8,
        "tools_started": 6,
        "tools_completed": 6,
        "hermes_tools_inflight": [],
    }
    events = [
        _event(1, "message.start"),
        _event(2, "message.complete", {"status": "complete"}),
    ]
    socket = _Socket(
        epoch="epoch-new",
        replays=[
            {"events": [], "latest_seq": 2},
            {"events": events, "latest_seq": 2},
        ],
        statuses=["idle", "idle"],
    )

    # Act
    observed = _observe(tmp_path, socket, previous)

    # Assert
    assert (
        observed.engine_incarnation_id,
        observed.turns_completed,
        [
            row["params"]["last_seen"]
            for row in socket.sent
            if row["method"] == "session.events.since"
        ],
    ) == ("generation-1:epoch-new:session-1", 1, [91, 0])


def test_recreated_live_session_resets_its_per_session_event_cursor(tmp_path):
    # Arrange
    _gateway(tmp_path)
    previous = {
        "writer": "hermes-session-events",
        "hermes_gateway_generation": "generation-1",
        "engine_incarnation_id": "generation-1:epoch-1:session-old",
        "hermes_event_epoch": "epoch-1",
        "hermes_event_seq": 91,
        "hermes_session_id": "session-old",
        "turns_accepted": 9,
        "turns_completed": 8,
        "tools_started": 6,
        "tools_completed": 6,
        "hermes_tools_inflight": [],
    }
    socket = _Socket(
        epoch="epoch-1",
        session_id="session-new",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["idle", "idle"],
    )

    # Act
    observed = _observe(tmp_path, socket, previous)

    # Assert
    assert (
        observed.engine_incarnation_id,
        observed.turns_completed,
        socket.sent[1]["params"]["last_seen"],
    ) == ("generation-1:epoch-1:session-new", 1, 0)


def test_first_observation_after_replay_eviction_starts_an_explicit_counter_scope(
    tmp_path,
):
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [_event(499, "thinking.delta"), _event(500, "tool.generating")],
                "latest_seq": 500,
                "truncated": True,
            }
        ],
        statuses=["working", "working"],
    )

    # Act
    observed = _observe(tmp_path, socket)

    # Assert
    assert (
        observed.event_seq,
        observed.turns_completed,
        observed.event_history_complete,
        observed.counter_scope_started_seq,
    ) == (500, 0, False, 500)


def test_owner_projection_persists_the_gap_free_event_cursor(tmp_path):
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["working", "idle"],
    )

    # Act
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *args, **kwargs: socket,
        now_fn=lambda: 100.0,
    )
    observed = read_hermes_heartbeat_projection(
        tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
    )

    # Assert
    assert (observed.event_seq, observed.turns_completed, observed.observed_at) == (
        2,
        1,
        100.0,
    )


def test_idle_projection_refresh_preserves_last_event_activity_time(tmp_path):
    # Arrange
    _gateway(tmp_path)
    first = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["working", "idle"],
    )
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *args, **kwargs: first,
        now_fn=lambda: 100.0,
    )
    second = _Socket(
        epoch="epoch-1",
        replays=[{"events": [], "latest_seq": 2}],
        statuses=["idle", "idle"],
    )

    # Act
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *args, **kwargs: second,
        now_fn=lambda: 103.0,
    )
    observed = read_hermes_heartbeat_projection(
        tmp_path, "scholar", previous=None, now_fn=lambda: 104.0
    )

    # Assert
    assert (observed.observed_at, observed.activity_at) == (103.0, 100.0)


def test_projection_from_a_replaced_gateway_is_rejected(tmp_path):
    # Arrange
    _gateway(tmp_path, generation="generation-old")
    socket = _Socket(
        epoch="epoch-old",
        replays=[{"events": [], "latest_seq": 0}],
        statuses=["idle", "idle"],
    )
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *args, **kwargs: socket,
        now_fn=lambda: 100.0,
    )
    _gateway(tmp_path, generation="generation-new")

    # Act
    def action():
        read_hermes_heartbeat_projection(
            tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
        )

    # Assert
    with pytest.raises(HermesHeartbeatProjectionError, match="previous gateway"):
        action()


def test_tui_writer_promotes_the_owner_projection_into_heartbeat_json(tmp_path):
    # Arrange
    _gateway(tmp_path)
    now = time.time()
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["working", "idle"],
    )
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *args, **kwargs: socket,
        now_fn=lambda: now,
    )
    agent = {
        "name": "scholar",
        "state_dir": tmp_path,
        "config": SimpleNamespace(harness="hermes"),
    }

    # Act
    written = _beat_one(
        agent,
        snapshot={"tui-scholar": 1_800_000_000},
        write_fn=write_heartbeat,
    )
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))

    # Assert
    assert (
        written,
        heartbeat["writer"],
        heartbeat["turns_completed"],
        heartbeat["engine_incarnation_id"],
        heartbeat["ts"],
    ) == (True, "hermes-session-events", 1, "generation-1:epoch-1:session-1", now)


def test_stale_heartbeat_from_previous_gateway_is_not_a_counter_baseline(tmp_path):
    # Arrange
    _gateway(tmp_path, generation="generation-new")
    previous = {
        "writer": "hermes-session-events",
        "hermes_gateway_generation": "generation-old",
        "engine_incarnation_id": "generation-old:epoch-old:session-old",
        "hermes_event_epoch": "epoch-old",
        "hermes_event_seq": 500,
        "hermes_session_id": "session-old",
        "turns_accepted": 100,
        "turns_completed": 100,
        "tools_started": 80,
        "tools_completed": 80,
        "hermes_tools_inflight": [],
    }
    socket = _Socket(
        epoch="epoch-new",
        replays=[
            {
                "events": [
                    _event(1, "message.start"),
                    _event(2, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["idle", "idle"],
    )

    # Act
    observed = _observe(tmp_path, socket, previous)

    # Assert
    assert (
        observed.engine_incarnation_id,
        observed.turns_completed,
        socket.sent[1]["params"]["last_seen"],
    ) == ("generation-new:epoch-new:session-1", 1, 0)
