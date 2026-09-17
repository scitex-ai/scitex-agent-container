"""Durable owner-side projection of Hermes' session-event cursor."""

from __future__ import annotations

import fcntl
import json
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .._runners._atomic import atomic_write_text
from ._hermes_heartbeat import (
    WRITER_HERMES_SESSION_EVENTS,
    HermesHeartbeatObservation,
    _assert_live_hermes_observation,
    _gateway_identity,
    observe_hermes_heartbeat,
)
from ._hermes_tui_owner import _gateway_state_lock
from ._hermes_tui_rpc import HermesTuiRpcError

PROJECTION_FILE = "hermes-heartbeat-events.json"
PROJECTION_LOCK_FILE = "hermes-heartbeat-events.lock"
PROJECTION_MAX_AGE_S = 15.0
_projection_thread_locks: dict[str, threading.Lock] = {}
_projection_thread_locks_guard = threading.Lock()


class HermesHeartbeatProjectionError(HermesTuiRpcError):
    """The event projection is absent, stale, or from another engine."""


@contextmanager
def _projection_lock(state_dir: Path):
    """Serialize one agent's read/advance/publish transaction across owners."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    key = str(state_dir.resolve())
    with _projection_thread_locks_guard:
        thread_lock = _projection_thread_locks.setdefault(key, threading.Lock())
    with thread_lock, open(state_dir / PROJECTION_LOCK_FILE, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_projection_payload(state_dir: Path) -> dict:
    try:
        payload = json.loads(
            (Path(state_dir) / PROJECTION_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError) as exc:
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection is unavailable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection is malformed"
        )
    return payload


def _projection_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection has invalid {key}: {value!r}"
        )
    return value


def _projection_text(
    payload: dict, key: str, *, allowed: set[str] | None = None
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or (allowed is not None and value not in allowed):
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection has invalid {key}: {value!r}"
        )
    return value


def read_hermes_heartbeat_projection(
    state_dir: Path,
    agent_name: str,
    *,
    previous: dict | None,
    max_age_s: float = PROJECTION_MAX_AGE_S,
    now_fn: Callable[[], float] = time.time,
) -> HermesHeartbeatObservation:
    """Read the owner-maintained event cursor, rejecting stale incarnations."""
    del previous
    state_dir = Path(state_dir)
    payload = _read_projection_payload(state_dir)
    identity = _gateway_identity(state_dir)
    if payload.get("hermes_gateway_generation") != identity.generation:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection belongs to a previous gateway"
        )
    if payload.get("agent_name") != agent_name:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection belongs to another agent"
        )
    observed_at = payload.get("observed_at")
    activity_at = payload.get("hermes_activity_at")
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(observed_at)
        or observed_at < 0
        or isinstance(activity_at, bool)
        or not isinstance(activity_at, (int, float))
        or not math.isfinite(activity_at)
        or activity_at < 0
        or activity_at > observed_at
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid observation time"
        )
    age = float(now_fn()) - float(observed_at)
    if age < -5.0 or age > float(max_age_s):
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection is stale ({age:.3f}s old)"
        )
    state = _projection_text(payload, "state", allowed={"ready", "busy"})
    epoch = _projection_text(payload, "hermes_event_epoch")
    engine = _projection_text(payload, "engine_incarnation_id")
    session_id = _projection_text(payload, "hermes_session_id")
    if (
        not epoch
        or engine != f"{identity.generation}:{epoch}:{session_id}"
        or not session_id
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid identity or state"
        )
    inflight = payload.get("hermes_tools_inflight")
    if not isinstance(inflight, list) or not all(
        isinstance(value, str) and value for value in inflight
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid tool cursor"
        )
    if len(inflight) != len(set(inflight)):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has duplicate tool ids"
        )
    tools_inflight = _projection_int(payload, "tools_inflight")
    if tools_inflight != len(inflight):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection tool count does not match its ids"
        )
    history_complete = payload.get("hermes_event_history_complete")
    if type(history_complete) is not bool:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid history completeness"
        )
    event_seq = _projection_int(payload, "hermes_event_seq")
    scope_started = _projection_int(payload, "hermes_counter_scope_started_seq")
    turns_accepted = _projection_int(payload, "turns_accepted")
    turns_completed = _projection_int(payload, "turns_completed")
    tools_started = _projection_int(payload, "tools_started")
    tools_completed = _projection_int(payload, "tools_completed")
    if scope_started > event_seq:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection counter scope exceeds event sequence"
        )
    if turns_completed > turns_accepted:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has impossible turn counters"
        )
    if tools_completed > tools_started:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has impossible tool counters"
        )
    if tools_inflight > tools_started - tools_completed:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has impossible inflight tool counters"
        )
    lifecycle_events = (
        turns_accepted + turns_completed + tools_started + tools_completed
    )
    if lifecycle_events > event_seq - scope_started:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection counters exceed their event scope"
        )
    last_event = _projection_text(payload, "last_event_type")
    last_status = _projection_text(
        payload,
        "last_turn_status",
        allowed={"", "complete", "error", "interrupted"},
    )
    return HermesHeartbeatObservation(
        observed_at=float(observed_at),
        activity_at=float(activity_at),
        state=str(state),
        engine_incarnation_id=str(engine),
        gateway_generation=identity.generation,
        event_epoch=epoch,
        event_seq=event_seq,
        event_history_complete=history_complete,
        counter_scope_started_seq=scope_started,
        session_id=session_id,
        turns_accepted=turns_accepted,
        turns_completed=turns_completed,
        tools_started=tools_started,
        tools_completed=tools_completed,
        tools_inflight=tools_inflight,
        inflight_tool_ids=tuple(sorted(set(inflight))),
        last_event_type=last_event,
        last_turn_status=last_status,
    )


def _assert_current_observation(
    state_dir: Path,
    agent_name: str,
    observed: HermesHeartbeatObservation,
) -> None:
    _assert_live_hermes_observation(state_dir, agent_name, observed)


def _heartbeat_is_observation(
    heartbeat_path: Path, observed: HermesHeartbeatObservation
) -> bool:
    try:
        value = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(
        isinstance(value, dict)
        and value.get("writer") == WRITER_HERMES_SESSION_EVENTS
        and value.get("engine_incarnation_id") == observed.engine_incarnation_id
        and value.get("hermes_event_seq") == observed.event_seq
        and value.get("hermes_session_id") == observed.session_id
        and value.get("hermes_gateway_generation")
        == observed.gateway_generation
    )


def _restore_heartbeat(
    heartbeat_path: Path,
    previous_bytes: bytes | None,
    observed: HermesHeartbeatObservation,
) -> None:
    """Retract only the stale publication this transaction still owns."""
    if not _heartbeat_is_observation(heartbeat_path, observed):
        return
    if previous_bytes is None:
        heartbeat_path.unlink(missing_ok=True)
    else:
        atomic_write_text(heartbeat_path, previous_bytes.decode("utf-8"))


def _assert_monotonic_publication(
    previous: dict | None, observed: HermesHeartbeatObservation
) -> None:
    if not isinstance(previous, dict) or previous.get(
        "writer"
    ) != WRITER_HERMES_SESSION_EVENTS:
        return
    if previous.get("engine_incarnation_id") != observed.engine_incarnation_id:
        return
    fields = {
        "hermes_event_seq": observed.event_seq,
        "turns_accepted": observed.turns_accepted,
        "turns_completed": observed.turns_completed,
        "tools_started": observed.tools_started,
        "tools_completed": observed.tools_completed,
    }
    for key, current in fields.items():
        baseline = previous.get(key)
        if type(baseline) is not int or baseline < 0 or current < baseline:
            raise HermesHeartbeatProjectionError(
                f"Hermes heartbeat publication would regress {key}"
            )


def promote_hermes_heartbeat_projection(
    state_dir: Path,
    agent_name: str,
    *,
    write_fn: Callable[..., None],
    observe_fn: Any = None,
) -> HermesHeartbeatObservation:
    """Publish one fenced projection or restore the prior heartbeat on a race."""
    from .._runners._session_state import read_heartbeat

    state_dir = Path(state_dir)
    heartbeat_path = state_dir / "heartbeat.json"
    observer = observe_fn or read_hermes_heartbeat_projection
    with _projection_lock(state_dir):
        previous = read_heartbeat(state_dir)
        observed = observer(state_dir, agent_name, previous=previous)
        previous_bytes = (
            heartbeat_path.read_bytes() if heartbeat_path.is_file() else None
        )
        # The owner uses this same lock while replacing its descriptor. The
        # checks on both sides also catch a crashed/adversarial writer that did
        # not cooperate with the lock.
        with _gateway_state_lock(state_dir):
            _assert_current_observation(state_dir, agent_name, observed)
            _assert_monotonic_publication(previous, observed)
            write_fn(
                state_dir,
                pid=0,
                state=observed.state,
                ts=observed.observed_at,
                writer=WRITER_HERMES_SESSION_EVENTS,
                authoritative_fields=observed.heartbeat_fields(),
            )
            try:
                _assert_current_observation(state_dir, agent_name, observed)
            except Exception:
                _restore_heartbeat(heartbeat_path, previous_bytes, observed)
                raise
        return observed


def refresh_hermes_heartbeat_projection(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
    now_fn: Callable[[], float] = time.time,
) -> HermesHeartbeatObservation:
    """Advance and atomically persist the owner-side Hermes event cursor."""
    state_dir = Path(state_dir)
    projection_path = state_dir / PROJECTION_FILE
    with _projection_lock(state_dir):
        if projection_path.exists():
            previous = _read_projection_payload(state_dir)
            current_generation = _gateway_identity(state_dir).generation
            if previous.get("hermes_gateway_generation") == current_generation:
                # A corrupt cursor from THIS engine must stop the bridge rather
                # than silently reset counters. A prior engine is intentionally
                # allowed through: observe_hermes_heartbeat fences it by identity.
                read_hermes_heartbeat_projection(
                    state_dir,
                    agent_name,
                    previous=None,
                    max_age_s=float("inf"),
                    now_fn=now_fn,
                )
        else:
            previous = None
        observed = observe_hermes_heartbeat(
            state_dir,
            agent_name,
            previous=previous,
            timeout_s=timeout_s,
            connect_fn=connect_fn,
            now_fn=now_fn,
        )
        payload = {
            "writer": WRITER_HERMES_SESSION_EVENTS,
            "agent_name": agent_name,
            "observed_at": observed.observed_at,
            "state": observed.state,
            **observed.heartbeat_fields(),
        }
        # Descriptor replacement uses this lock too. Once observe() has fenced
        # the RPC transaction, no new owner can interleave before publication.
        with _gateway_state_lock(state_dir):
            if _gateway_identity(state_dir).generation != observed.gateway_generation:
                raise HermesHeartbeatProjectionError(
                    "Hermes gateway incarnation changed before projection publication"
                )
            atomic_write_text(projection_path, json.dumps(payload))
        return observed


__all__ = [
    "HermesHeartbeatProjectionError",
    "PROJECTION_FILE",
    "promote_hermes_heartbeat_projection",
    "read_hermes_heartbeat_projection",
    "refresh_hermes_heartbeat_projection",
]
