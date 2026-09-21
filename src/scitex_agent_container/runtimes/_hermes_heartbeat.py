"""Authoritative Hermes session-event projection for ``heartbeat.json``.

Hermes' gateway owns the accepted-turn and streamed-tool lifecycle.  SAC must
therefore read ``session.events.since`` rather than infer progress from tmux
pane activity or SDK ``quota.json`` files Hermes never writes.  A successful
observation is reduced into monotonic counters for exactly one gateway replay
epoch.  Any RPC failure, replay gap, identity race, or malformed event raises;
the caller then preserves the last heartbeat byte-for-byte.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ._hermes_tui_owner import GATEWAY_FILE
from ._hermes_tui_rpc import (
    HermesTuiRpcError,
    _connect,
    _rpc,
    _select_session_row,
)

WRITER_HERMES_SESSION_EVENTS = "hermes-session-events"


@dataclass(frozen=True)
class HermesHeartbeatObservation:
    """One gap-free projection of Hermes' live session-event stream."""

    observed_at: float
    activity_at: float
    state: str
    engine_incarnation_id: str
    gateway_generation: str
    event_epoch: str
    event_seq: int
    event_history_complete: bool
    counter_scope_started_seq: int
    session_id: str
    turns_accepted: int
    turns_completed: int
    tools_started: int
    tools_completed: int
    tools_inflight: int
    inflight_tool_ids: tuple[str, ...]
    last_event_type: str
    last_turn_status: str

    def heartbeat_fields(self) -> dict[str, object]:
        """Fields that replace SDK-only quota inference in the shared writer."""
        return {
            "engine_incarnation_id": self.engine_incarnation_id,
            "hermes_activity_at": self.activity_at,
            "hermes_gateway_generation": self.gateway_generation,
            "hermes_event_epoch": self.event_epoch,
            "hermes_event_seq": self.event_seq,
            "hermes_event_history_complete": self.event_history_complete,
            "hermes_counter_scope_started_seq": self.counter_scope_started_seq,
            "hermes_session_id": self.session_id,
            "turns_accepted": self.turns_accepted,
            "turns_completed": self.turns_completed,
            "tools_started": self.tools_started,
            "tools_completed": self.tools_completed,
            "tools_inflight": self.tools_inflight,
            "hermes_tools_inflight": list(self.inflight_tool_ids),
            "last_event_type": self.last_event_type,
            "last_turn_status": self.last_turn_status,
        }


@dataclass(frozen=True)
class _GatewayIdentity:
    generation: str
    port: int
    token: str

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token={self.token}"


def _gateway_identity(state_dir: Path) -> _GatewayIdentity:
    try:
        descriptor = json.loads((state_dir / GATEWAY_FILE).read_text(encoding="utf-8"))
        generation = str(descriptor["generation"]).strip()
        port = int(descriptor["port"])
        token = (state_dir / "hermes-api.key").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HermesTuiRpcError(f"Hermes gateway identity is unavailable: {exc}") from exc
    if not generation or not 0 < port < 65536 or len(token) < 16:
        raise HermesTuiRpcError("Hermes gateway identity is invalid")
    return _GatewayIdentity(generation, port, token)


def _previous_watermark(
    previous: dict | None, generation: str
) -> tuple[int, str, str]:
    if not isinstance(previous, dict):
        return 0, "", ""
    if (
        previous.get("writer") != WRITER_HERMES_SESSION_EVENTS
        or previous.get("hermes_gateway_generation") != generation
    ):
        return 0, "", ""
    seq = previous.get("hermes_event_seq")
    epoch = previous.get("hermes_event_epoch")
    session_id = previous.get("hermes_session_id")
    if (
        type(seq) is not int
        or seq < 0
        or not isinstance(epoch, str)
        or not epoch
        or not isinstance(session_id, str)
        or not session_id
    ):
        return 0, "", ""
    return seq, epoch, session_id


def _parse_replay(
    replay: dict,
    *,
    after_seq: int,
    expected_epoch: str = "",
    allow_truncated_baseline: bool = False,
) -> tuple[list[dict], int, str, bool]:
    events = replay.get("events")
    latest_seq = replay.get("latest_seq")
    epoch = replay.get("epoch")
    truncated = replay.get("truncated")
    if (
        not isinstance(events, list)
        or type(latest_seq) is not int
        or latest_seq < 0
        or not isinstance(epoch, str)
        or not epoch
        or type(truncated) is not bool
    ):
        raise HermesTuiRpcError(
            f"Hermes session.events.since returned malformed result: {replay!r}"
        )
    if expected_epoch and epoch != expected_epoch:
        return events, latest_seq, epoch, truncated
    if truncated:
        if allow_truncated_baseline:
            # Replay is a bounded ring, not a historical database. Begin an
            # explicit exact counter scope at the current watermark rather
            # than infer old turns or consume a partial lifecycle.
            return [], latest_seq, epoch, True
        raise HermesTuiRpcError(
            "Hermes session event replay is truncated; refusing a partial heartbeat"
        )
    if latest_seq < after_seq:
        raise HermesTuiRpcError("Hermes session event sequence moved backwards")
    expected = list(range(after_seq + 1, latest_seq + 1))
    actual = []
    for event in events:
        seq = event.get("seq") if isinstance(event, dict) else None
        if type(seq) is not int or seq < 0:
            raise HermesTuiRpcError(
                f"Hermes event replay contained malformed event sequence: {event!r}"
            )
        actual.append(seq)
    if actual != expected:
        raise HermesTuiRpcError(
            f"Hermes session event replay has a gap: expected {expected!r}, got {actual!r}"
        )
    return events, latest_seq, epoch, False


def _counter(previous: dict | None, key: str, *, same_engine: bool) -> int:
    if not same_engine or not isinstance(previous, dict):
        return 0
    value = previous.get(key)
    return value if type(value) is int and value >= 0 else 0


def _inflight(previous: dict | None, *, same_engine: bool) -> set[str]:
    if not same_engine or not isinstance(previous, dict):
        return set()
    values = previous.get("hermes_tools_inflight")
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        return set()
    return set(values)


def _reduce(
    events: list[dict],
    *,
    previous: dict | None,
    same_engine: bool,
) -> tuple[int, int, int, int, set[str], str, str]:
    accepted = _counter(previous, "turns_accepted", same_engine=same_engine)
    completed = _counter(previous, "turns_completed", same_engine=same_engine)
    tools_started = _counter(previous, "tools_started", same_engine=same_engine)
    tools_completed = _counter(previous, "tools_completed", same_engine=same_engine)
    inflight = _inflight(previous, same_engine=same_engine)
    last_event = (
        str(previous.get("last_event_type") or "")
        if same_engine and isinstance(previous, dict)
        else ""
    )
    last_status = (
        str(previous.get("last_turn_status") or "")
        if same_engine and isinstance(previous, dict)
        else ""
    )
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(event_type, str) or not event_type:
            raise HermesTuiRpcError(f"Hermes event replay contained malformed event: {event!r}")
        if not isinstance(payload, dict):
            # A KNOWN event type may legitimately arrive without a payload: the
            # gateway emits e.g. {'type': 'message.start', 'session_id': ...,
            # 'seq': ...}. Refusing the whole replay for that made the projection
            # raise on EVERY retry, so the heartbeat never advanced, the registry
            # reported the agent stopped, and six agents sat in a 10,000-line
            # retry loop for ~11 hours (2026-09-20 incident). Skip the event and
            # keep projecting; the replay-level guards above (truncation,
            # backwards sequence, non-int seq) still refuse a projection that
            # cannot be trusted, which is where refusing is genuinely right.
            continue
        last_event = event_type
        if event_type == "message.start":
            accepted += 1
        elif event_type == "message.complete":
            status = payload.get("status")
            if status not in {"complete", "error", "interrupted"}:
                # Same distinction: one unusable event is skipped rather than
                # making the entire replay unprojectable for the next 11 hours.
                continue
            # A bounded initial replay can begin in the middle of a turn. Its
            # terminal event advances the cursor but cannot complete a turn in
            # the exact counter scope unless a matching start is known.
            if completed < accepted:
                completed += 1
            last_status = str(status)
            inflight.clear()
        elif event_type in {"tool.start", "tool.complete"}:
            tool_id = payload.get("tool_id")
            if not isinstance(tool_id, str) or not tool_id:
                raise HermesTuiRpcError(
                    f"Hermes {event_type} had no stable tool id: {event!r}"
                )
            if event_type == "tool.start":
                tools_started += 1
                inflight.add(tool_id)
            elif tool_id in inflight:
                # As with turns, ignore an orphan terminal from before a
                # truncated baseline rather than manufacture an impossible
                # completed > started projection.
                tools_completed += 1
                inflight.discard(tool_id)
    return (
        accepted,
        completed,
        tools_started,
        tools_completed,
        inflight,
        last_event,
        last_status,
    )


def _state_from_row(row: dict) -> str:
    status = str(row.get("status") or "").strip().lower()
    if status == "idle":
        return "ready"
    if status in {"working", "waiting", "starting", "streaming", "resuming"}:
        return "busy"
    raise HermesTuiRpcError(
        f"Hermes session {row.get('id')!r} returned unknown heartbeat status {status!r}"
    )


def _assert_live_hermes_observation(
    state_dir: Path,
    agent_name: str,
    observed: HermesHeartbeatObservation,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> None:
    """Fence promotion against both gateway and authoritative live-session identity."""
    state_dir = Path(state_dir)
    identity = _gateway_identity(state_dir)
    if identity.generation != observed.gateway_generation:
        raise HermesTuiRpcError(
            "Hermes gateway incarnation changed before heartbeat publication"
        )
    try:
        with _connect(identity.url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            row = _select_session_row(
                listing.get("sessions"), f"sac:{agent_name}"
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes live-session fence at {identity.url.split('?')[0]} failed: {exc}"
        ) from exc
    final_identity = _gateway_identity(state_dir)
    if final_identity != identity:
        raise HermesTuiRpcError(
            "Hermes gateway incarnation changed during live-session fence"
        )
    if str(row.get("id") or "") != observed.session_id:
        raise HermesTuiRpcError(
            "Hermes heartbeat projection is not the authoritative live session"
        )


def observe_hermes_heartbeat(
    state_dir: Path,
    agent_name: str,
    *,
    previous: dict | None,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
    now_fn: Callable[[], float] = time.time,
) -> HermesHeartbeatObservation:
    """Project one exact Hermes engine incarnation into heartbeat counters.

    The gateway descriptor is read before and after the RPC transaction.  A
    replacement owner therefore cannot publish a stale old-engine observation.
    Replay-epoch changes are retried from sequence zero; old heartbeat counters
    are never reused across that incarnation boundary.
    """
    state_dir = Path(state_dir)
    identity = _gateway_identity(state_dir)
    after_seq, previous_epoch, previous_session = _previous_watermark(
        previous, identity.generation
    )
    try:
        with _connect(identity.url, timeout_s, connect_fn) as socket:
            first_listing = _rpc(socket, 1, "session.active_list", {})
            first_row = _select_session_row(
                first_listing.get("sessions"), f"sac:{agent_name}"
            )
            session_id = str(first_row["id"])
            if previous_session and previous_session != session_id:
                after_seq, previous_epoch = 0, ""
            replay = _rpc(
                socket,
                2,
                "session.events.since",
                {"session_id": session_id, "last_seen": after_seq},
            )
            events, latest_seq, epoch, bootstrapped = _parse_replay(
                replay,
                after_seq=after_seq,
                expected_epoch=previous_epoch,
                allow_truncated_baseline=not previous_epoch,
            )
            request_id = 3
            if previous_epoch and epoch != previous_epoch:
                after_seq = 0
                replay = _rpc(
                    socket,
                    request_id,
                    "session.events.since",
                    {"session_id": session_id, "last_seen": 0},
                )
                request_id += 1
                events, latest_seq, epoch, bootstrapped = _parse_replay(
                    replay, after_seq=0, allow_truncated_baseline=True
                )
            final_listing = _rpc(socket, request_id, "session.active_list", {})
            final_row = _select_session_row(
                final_listing.get("sessions"), f"sac:{agent_name}"
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes heartbeat observation at {identity.url.split('?')[0]} failed: {exc}"
        ) from exc

    final_identity = _gateway_identity(state_dir)
    if final_identity != identity:
        raise HermesTuiRpcError(
            "Hermes gateway incarnation changed during heartbeat observation"
        )
    if str(final_row.get("id") or "") != session_id:
        raise HermesTuiRpcError(
            "Hermes live session changed during heartbeat observation"
        )

    engine_incarnation = f"{identity.generation}:{epoch}:{session_id}"
    same_engine = bool(
        isinstance(previous, dict)
        and previous.get("engine_incarnation_id") == engine_incarnation
    )
    history_complete = (
        bool(previous.get("hermes_event_history_complete", True))
        if same_engine and isinstance(previous, dict)
        else not bootstrapped
    )
    scope_started = (
        _counter(previous, "hermes_counter_scope_started_seq", same_engine=True)
        if same_engine
        else latest_seq if bootstrapped else 0
    )
    observed_at = float(now_fn())
    previous_activity = (
        previous.get("hermes_activity_at") if isinstance(previous, dict) else None
    )
    activity_at = (
        float(previous_activity)
        if same_engine
        and not events
        and isinstance(previous_activity, (int, float))
        and previous_activity >= 0
        else observed_at
    )
    (
        accepted,
        completed,
        tools_started,
        tools_completed,
        inflight,
        last_event,
        last_status,
    ) = _reduce(events, previous=previous, same_engine=same_engine)
    return HermesHeartbeatObservation(
        observed_at=observed_at,
        activity_at=activity_at,
        state=_state_from_row(final_row),
        engine_incarnation_id=engine_incarnation,
        gateway_generation=identity.generation,
        event_epoch=epoch,
        event_seq=latest_seq,
        event_history_complete=history_complete,
        counter_scope_started_seq=scope_started,
        session_id=session_id,
        turns_accepted=accepted,
        turns_completed=completed,
        tools_started=tools_started,
        tools_completed=tools_completed,
        tools_inflight=len(inflight),
        inflight_tool_ids=tuple(sorted(inflight)),
        last_event_type=last_event,
        last_turn_status=last_status,
    )


__all__ = [
    "HermesHeartbeatObservation",
    "WRITER_HERMES_SESSION_EVENTS",
    "observe_hermes_heartbeat",
]
