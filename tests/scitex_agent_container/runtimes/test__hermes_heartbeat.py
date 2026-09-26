"""Hermes session events are the heartbeat's authoritative turn instrument."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._tui_heartbeat_loop import _beat_one
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container.runtimes._hermes_heartbeat import (
    HermesHeartbeatObservation,
    _parse_replay,
    observe_hermes_heartbeat,
)
from scitex_agent_container.runtimes._hermes_heartbeat_projection import (
    HermesHeartbeatProjectionError,
    read_hermes_heartbeat_projection,
    refresh_hermes_heartbeat_projection,
)
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTuiRpcError


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


def _valid_projection() -> dict:
    return {
        "writer": "hermes-session-events",
        "agent_name": "scholar",
        "observed_at": 100.0,
        "state": "ready",
        "engine_incarnation_id": "generation-1:epoch-1:session-1",
        "hermes_activity_at": 99.0,
        "hermes_gateway_generation": "generation-1",
        "hermes_event_epoch": "epoch-1",
        "hermes_event_seq": 4,
        "hermes_event_history_complete": True,
        "hermes_counter_scope_started_seq": 0,
        "hermes_session_id": "session-1",
        "turns_accepted": 1,
        "turns_completed": 1,
        "tools_started": 1,
        "tools_completed": 1,
        "tools_inflight": 0,
        "hermes_tools_inflight": [],
        "last_event_type": "message.complete",
        "last_turn_status": "complete",
    }


REQUIRED_PROJECTION_FIELDS = tuple(_valid_projection())


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"observed_at": float("nan")}, "observation time"),
        ({"hermes_activity_at": float("inf")}, "observation time"),
        ({"last_event_type": {"bad": "shape"}}, "last_event_type"),
        ({"last_turn_status": 1}, "last_turn_status"),
        ({"last_turn_status": "successful"}, "last_turn_status"),
        (
            {
                "tools_started": 2,
                "tools_inflight": 1,
                "hermes_tools_inflight": ["duplicate", "duplicate"],
            },
            "duplicate tool",
        ),
        ({"hermes_counter_scope_started_seq": 5}, "counter scope"),
        ({"turns_accepted": 1, "turns_completed": 2}, "turn counters"),
        ({"tools_started": 1, "tools_completed": 2}, "tool counters"),
        (
            {
                "tools_started": 2,
                "tools_completed": 1,
                "tools_inflight": 2,
                "hermes_tools_inflight": ["call-1", "call-2"],
            },
            "inflight tool counters",
        ),
    ],
)
def test_malformed_projection_is_rejected(tmp_path, updates, reason):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection.update(updates)
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )

    # Act
    def action():
        read_hermes_heartbeat_projection(
            tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
        )

    # Assert
    with pytest.raises(HermesHeartbeatProjectionError, match=reason):
        action()


@pytest.mark.parametrize("field", REQUIRED_PROJECTION_FIELDS)
def test_projection_rejects_every_missing_required_field(tmp_path, field):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection.pop(field)
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )

    # Act
    # Assert
    with pytest.raises(HermesHeartbeatProjectionError):
        read_hermes_heartbeat_projection(
            tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
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


def test_a_known_event_without_a_payload_is_skipped_not_fatal(tmp_path):
    """One unusable event must not make the WHOLE replay unprojectable.

    Regression for the 2026-09-20 incident: the gateway emits
    ``{'type': 'message.start', 'session_id': ..., 'seq': N}`` with no
    ``payload``, the projection raised on that shape, and because the caller
    retries, six agents sat in a ~11-hour loop with 10,000+ identical lines
    while the registry reported them stopped. An unknown event TYPE is still
    refused; a known type with nothing to read from is skipped, so the
    projection continues and the heartbeat advances.
    """
    # Arrange
    _gateway(tmp_path)
    socket = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    {"seq": 1, "type": "message.start"},  # no payload: incident shape
                    _event(2, "message.start"),
                ],
                "latest_seq": 2,
            }
        ],
        statuses=["working", "working"],
    )

    # Act
    observed = _observe(tmp_path, socket)

    # Assert
    assert (observed.state, observed.turns_accepted) == ("busy", 1)


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


def test_live_session_rpc_failure_leaves_last_heartbeat_untouched(tmp_path):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection["observed_at"] = time.time()
    projection["hermes_activity_at"] = projection["observed_at"]
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    heartbeat = tmp_path / "heartbeat.json"
    original = b'{"writer":"hermes-session-events","turns_completed":4}'
    heartbeat.write_bytes(original)
    agent = {
        "name": "scholar",
        "state_dir": tmp_path,
        "config": SimpleNamespace(harness="hermes"),
    }

    class BrokenSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, _raw):
            return None

        def recv(self):
            raise OSError("gateway RPC failed")

    # Act
    written = _beat_one(
        agent,
        snapshot={"tui-scholar": 1_800_000_000},
        write_fn=write_heartbeat,
        hermes_connect_fn=lambda *_args, **_kwargs: BrokenSocket(),
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


def test_orphan_terminal_after_truncated_baseline_does_not_poison_projection(
    tmp_path,
):
    # Arrange — seq 500 may be in the middle of the turn that ends at seq 501.
    _gateway(tmp_path)
    baseline = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [_event(500, "thinking.delta")],
                "latest_seq": 500,
                "truncated": True,
            }
        ],
        statuses=["working", "working"],
    )
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *_args, **_kwargs: baseline,
        now_fn=lambda: 100.0,
    )
    terminal = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [_event(501, "message.complete", {"status": "complete"})],
                "latest_seq": 501,
            }
        ],
        statuses=["idle", "idle"],
    )

    # Act
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *_args, **_kwargs: terminal,
        now_fn=lambda: 101.0,
    )
    observed = read_hermes_heartbeat_projection(
        tmp_path, "scholar", previous=None, now_fn=lambda: 102.0
    )

    # Assert — the unmatched completion is outside the known counter scope.
    assert (
        observed.event_seq,
        observed.turns_accepted,
        observed.turns_completed,
        observed.event_history_complete,
        observed.counter_scope_started_seq,
    ) == (501, 0, 0, False, 500)


def test_only_matched_lifecycles_count_after_a_truncated_baseline(tmp_path):
    # Arrange
    _gateway(tmp_path)
    baseline = _Socket(
        epoch="epoch-1",
        replays=[{"events": [], "latest_seq": 500, "truncated": True}],
        statuses=["working", "working"],
    )
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *_args, **_kwargs: baseline,
        now_fn=lambda: 100.0,
    )
    replay = _Socket(
        epoch="epoch-1",
        replays=[
            {
                "events": [
                    _event(501, "message.complete", {"status": "complete"}),
                    _event(502, "tool.complete", {"tool_id": "orphan"}),
                    _event(503, "message.start"),
                    _event(504, "tool.start", {"tool_id": "matched"}),
                    _event(505, "tool.complete", {"tool_id": "matched"}),
                    _event(506, "message.complete", {"status": "complete"}),
                ],
                "latest_seq": 506,
            }
        ],
        statuses=["working", "idle"],
    )

    # Act
    refresh_hermes_heartbeat_projection(
        tmp_path,
        "scholar",
        connect_fn=lambda *_args, **_kwargs: replay,
        now_fn=lambda: 101.0,
    )
    observed = read_hermes_heartbeat_projection(
        tmp_path, "scholar", previous=None, now_fn=lambda: 102.0
    )

    # Assert
    assert (
        observed.turns_accepted,
        observed.turns_completed,
        observed.tools_started,
        observed.tools_completed,
        observed.event_history_complete,
    ) == (1, 1, 1, 1, False)


@pytest.mark.parametrize("event_seq", [True, False, 1.0, "1", -1])
def test_replay_event_sequence_requires_a_non_negative_exact_int(event_seq):
    # Arrange
    replay = {
        "events": [{"seq": event_seq, "type": "thinking.delta", "payload": {}}],
        "latest_seq": 1,
        "epoch": "epoch-1",
        "truncated": False,
    }

    # Act — type validation precedes equality-compatible continuity checks.
    # Assert
    with pytest.raises(HermesTuiRpcError, match="malformed event sequence"):
        _parse_replay(replay, after_seq=0)


@pytest.mark.parametrize("writer", [True, False, 1, 1.0, None, "other-writer"])
def test_projection_writer_requires_the_exact_text_identity(tmp_path, writer):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection["writer"] = writer
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )

    # Act
    # Assert
    with pytest.raises(HermesHeartbeatProjectionError, match="invalid writer"):
        read_hermes_heartbeat_projection(
            tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
        )


def test_invalid_observation_is_rejected_before_atomic_projection_publication(tmp_path):
    # Arrange
    _gateway(tmp_path)
    impossible = HermesHeartbeatObservation(
        observed_at=100.0,
        activity_at=100.0,
        state="ready",
        engine_incarnation_id="generation-1:epoch-1:session-1",
        gateway_generation="generation-1",
        event_epoch="epoch-1",
        event_seq=1,
        event_history_complete=True,
        counter_scope_started_seq=0,
        session_id="session-1",
        turns_accepted=0,
        turns_completed=1,
        tools_started=0,
        tools_completed=0,
        tools_inflight=0,
        inflight_tool_ids=(),
        last_event_type="message.complete",
        last_turn_status="complete",
    )
    projection_path = tmp_path / "hermes-heartbeat-events.json"

    # Act
    try:
        refresh_hermes_heartbeat_projection(
            tmp_path,
            "scholar",
            observe_fn=lambda *_args, **_kwargs: impossible,
            now_fn=lambda: 100.0,
        )
    except HermesHeartbeatProjectionError as exc:
        error = exc
    else:
        error = None

    # Assert
    assert (
        isinstance(error, HermesHeartbeatProjectionError),
        "impossible turn counters" in str(error),
        projection_path.exists(),
    ) == (True, True, False)


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


def test_published_projection_is_canonical_json_and_round_trips(tmp_path):
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
        connect_fn=lambda *_args, **_kwargs: socket,
        now_fn=lambda: 100.0,
    )
    raw = (tmp_path / "hermes-heartbeat-events.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    observed = read_hermes_heartbeat_projection(
        tmp_path, "scholar", previous=None, now_fn=lambda: 101.0
    )

    # Assert
    assert (
        raw,
        observed.event_seq,
        observed.turns_accepted,
        observed.turns_completed,
    ) == (
        json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")),
        2,
        1,
        1,
    )


def test_replay_gap_preserves_previous_projection_byte_for_byte(tmp_path):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection["observed_at"] = time.time()
    projection["hermes_activity_at"] = projection["observed_at"]
    projection_path = tmp_path / "hermes-heartbeat-events.json"
    original = json.dumps(projection, separators=(",", ":")).encode()
    projection_path.write_bytes(original)
    socket = _Socket(
        epoch="epoch-1",
        replays=[{"events": [_event(6, "thinking.delta")], "latest_seq": 6}],
        statuses=["idle"],
    )

    # Act
    try:
        refresh_hermes_heartbeat_projection(
            tmp_path,
            "scholar",
            connect_fn=lambda *_args, **_kwargs: socket,
        )
    except HermesTuiRpcError as exc:
        error = exc
    else:
        error = None

    # Assert
    assert (
        isinstance(error, HermesTuiRpcError),
        "replay has a gap" in str(error),
        projection_path.read_bytes(),
    ) == (True, True, original)


def test_overlapping_projection_refreshes_cannot_regress_event_sequence(tmp_path):
    # Arrange
    _gateway(tmp_path)
    first_started = threading.Event()
    newer_finished = threading.Event()
    errors: list[BaseException] = []

    def observation(seq: int) -> HermesHeartbeatObservation:
        return HermesHeartbeatObservation(
            observed_at=100.0 + seq,
            activity_at=100.0 + seq,
            state="ready",
            engine_incarnation_id="generation-1:epoch-1:session-1",
            gateway_generation="generation-1",
            event_epoch="epoch-1",
            event_seq=seq,
            event_history_complete=True,
            counter_scope_started_seq=0,
            session_id="session-1",
            turns_accepted=0,
            turns_completed=0,
            tools_started=0,
            tools_completed=0,
            tools_inflight=0,
            inflight_tool_ids=(),
            last_event_type="",
            last_turn_status="",
        )

    def observe(*_args, previous=None, **_kwargs):
        if threading.current_thread().name == "stale-owner":
            first_started.set()
            newer_finished.wait(timeout=0.2)
            return observation(1)
        # Without serialization both owners read the empty baseline and this
        # newer owner publishes first. With serialization it sees seq=1.
        if previous is not None and previous["hermes_event_seq"] != 1:
            raise AssertionError("serialized owner did not observe sequence 1")
        newer_finished.set()
        return observation(2)

    def refresh():
        try:
            refresh_hermes_heartbeat_projection(
                tmp_path, "scholar", observe_fn=observe
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    stale = threading.Thread(target=refresh, name="stale-owner")
    newer = threading.Thread(target=refresh, name="newer-owner")

    # Act
    stale.start()
    first_started_seen = first_started.wait(timeout=1)
    newer.start()
    stale.join(timeout=2)
    newer.join(timeout=2)
    projection = json.loads(
        (tmp_path / "hermes-heartbeat-events.json").read_text(encoding="utf-8")
    )

    # Assert
    assert (
        first_started_seen,
        errors,
        stale.is_alive(),
        newer.is_alive(),
        projection["hermes_event_seq"],
    ) == (True, [], False, False, 2)


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


def test_gateway_replacement_after_projection_read_prevents_heartbeat_write(tmp_path):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection["observed_at"] = time.time()
    projection["hermes_activity_at"] = projection["observed_at"] - 10
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    original = b'{"writer":"hermes-session-events","hermes_event_seq":3}'
    (tmp_path / "heartbeat.json").write_bytes(original)
    agent = {
        "name": "scholar",
        "state_dir": tmp_path,
        "config": SimpleNamespace(harness="hermes"),
    }

    def replace_after_read(*args, **kwargs):
        observed = read_hermes_heartbeat_projection(*args, **kwargs)
        _gateway(tmp_path, generation="generation-2")
        return observed

    # Act
    written = _beat_one(
        agent,
        snapshot={"tui-scholar": 1_800_000_000},
        write_fn=write_heartbeat,
        hermes_observe_fn=replace_after_read,
        hermes_connect_fn=lambda *_args, **_kwargs: _Socket(
            epoch="epoch-1", replays=[], statuses=["idle"]
        ),
    )

    # Assert
    assert (written, (tmp_path / "heartbeat.json").read_bytes()) == (False, original)


def test_gateway_replacement_during_publication_retracts_stale_heartbeat(tmp_path):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    projection["observed_at"] = time.time()
    projection["hermes_activity_at"] = projection["observed_at"] - 10
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    original = b'{"writer":"hermes-session-events","hermes_event_seq":3}'
    (tmp_path / "heartbeat.json").write_bytes(original)
    agent = {
        "name": "scholar",
        "state_dir": tmp_path,
        "config": SimpleNamespace(harness="hermes"),
    }
    socket = _Socket(
        epoch="epoch-1", replays=[], statuses=["idle", "idle"]
    )

    def replace_while_writing(*args, **kwargs):
        write_heartbeat(*args, **kwargs)
        _gateway(tmp_path, generation="generation-2")

    # Act
    written = _beat_one(
        agent,
        snapshot={"tui-scholar": 1_800_000_000},
        write_fn=replace_while_writing,
        hermes_connect_fn=lambda *_args, **_kwargs: socket,
    )

    # Assert
    assert (written, (tmp_path / "heartbeat.json").read_bytes()) == (False, original)


def test_stale_same_generation_session_projection_cannot_overwrite_current_session(
    tmp_path,
):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    now = time.time()
    projection.update(
        {
            "observed_at": now,
            "hermes_activity_at": now - 10,
            "engine_incarnation_id": "generation-1:epoch-1:session-old",
            "hermes_session_id": "session-old",
        }
    )
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    original = b'{"writer":"hermes-session-events","hermes_gateway_generation":"generation-1","engine_incarnation_id":"generation-1:epoch-1:session-new","hermes_session_id":"session-new","hermes_event_seq":10}'
    (tmp_path / "heartbeat.json").write_bytes(original)
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
        hermes_connect_fn=lambda *_args, **_kwargs: _Socket(
            epoch="epoch-1",
            replays=[],
            statuses=["idle", "idle"],
            session_id="session-new",
        ),
    )

    # Assert
    assert (written, (tmp_path / "heartbeat.json").read_bytes()) == (False, original)


def test_same_engine_projection_cannot_regress_published_sequence(tmp_path):
    # Arrange
    _gateway(tmp_path)
    projection = _valid_projection()
    now = time.time()
    projection.update({"observed_at": now, "hermes_activity_at": now})
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    previous = {
        **projection,
        "hermes_event_seq": 5,
        "writer": "hermes-session-events",
    }
    original = json.dumps(previous, separators=(",", ":")).encode()
    (tmp_path / "heartbeat.json").write_bytes(original)
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
        hermes_connect_fn=lambda *_args, **_kwargs: _Socket(
            epoch="epoch-1", replays=[], statuses=["idle", "idle"]
        ),
    )

    # Assert
    assert (written, (tmp_path / "heartbeat.json").read_bytes()) == (False, original)


def test_heartbeat_liveness_is_fresh_while_hermes_activity_remains_old(tmp_path):
    # Arrange
    _gateway(tmp_path)
    now = time.time()
    old_activity = now - 300
    projection = _valid_projection()
    projection.update({"observed_at": now, "hermes_activity_at": old_activity})
    (tmp_path / "hermes-heartbeat-events.json").write_text(
        json.dumps(projection), encoding="utf-8"
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
        hermes_connect_fn=lambda *_args, **_kwargs: _Socket(
            epoch="epoch-1", replays=[], statuses=["idle", "idle"]
        ),
    )
    heartbeat = json.loads(
        (tmp_path / "heartbeat.json").read_text(encoding="utf-8")
    )

    # Assert
    assert (written, heartbeat["ts"], heartbeat["hermes_activity_at"]) == (
        True,
        now,
        old_activity,
    )


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
        hermes_connect_fn=lambda *_args, **_kwargs: _Socket(
            epoch="epoch-1", replays=[], statuses=["idle", "idle"]
        ),
    )
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    resident = heartbeat["authoritative_heartbeat"]

    # Assert
    assert (
        written,
        heartbeat["writer"],
        heartbeat["turns_completed"],
        heartbeat["engine_incarnation_id"],
        heartbeat["ts"],
        resident["agent_id"],
        resident["session_id"],
        resident["boot_id"],
        resident["progress_seq"],
        resident["state"],
    ) == (
        True,
        "hermes-session-events",
        1,
        "generation-1:epoch-1:session-1",
        now,
        "scholar",
        "session-1",
        "generation-1:epoch-1:session-1",
        2,
        "idle",
    )


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
