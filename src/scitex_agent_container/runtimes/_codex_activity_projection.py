"""Publish fenced native activity through the existing heartbeat surface."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable

from .._runners._atomic import atomic_write_text
from ._activity_source_identity import activity_source_id
from ._codex_activity import (
    CodexActivityError,
    CodexActivityObservation,
    read_codex_activity,
)
from ._codex_activity_binding import assert_codex_binding_current, bind_codex_runtime

WRITER_CODEX_ROLLOUT = "codex-rollout-events"


def _activity_source_id(binding):
    return activity_source_id(
        {
            "agent": binding.agent_name,
            "host": binding.host,
            "instance_id": binding.instance_id,
            "boot_id": binding.boot_id,
            "session_id": binding.thread_id,
        },
        binding.rollout_identity,
    )


def activity_harness(
    state_dir: Path,
    agent_name: str,
    *,
    host: str,
    declared_harness: str,
    birth_reader: Callable | None = None,
) -> str:
    """Select the activity instrument from the canonical immutable launch."""
    from .._runners._session_state import read_instance_id

    instance_id = read_instance_id(Path(state_dir))
    if not instance_id:
        return declared_harness
    if birth_reader is None:
        from .._state.state_store_incarnations import get_incarnation as birth_reader
    birth = birth_reader(instance_id)
    if birth is None:
        return declared_harness
    if not isinstance(birth, dict) or (
        birth.get("incarnation_id") != instance_id
        or birth.get("agent_id") != agent_name
        or birth.get("host") != host
    ):
        raise CodexActivityError("native activity launch selector has wrong ownership")
    try:
        from ..config._harness_lookup import canonical_harness

        compiled = json.loads(birth["compiled_spec_json"])
        harness = compiled["harness"]
        if (
            compiled.get("name") != agent_name
            or compiled.get("runtime") != "tui"
            or not isinstance(harness, str)
            or not harness
        ):
            raise ValueError("invalid launch harness")
        harness = canonical_harness(harness)
        if harness is None:
            raise ValueError("unknown launch harness")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise CodexActivityError(
            "native activity launch selector is malformed"
        ) from exc
    return harness.strip().lower()


def _launch_identity(
    birth: dict | None, *, instance_id: str, agent_name: str, host: str
) -> dict:
    if not isinstance(birth, dict) or (
        birth.get("incarnation_id") != instance_id
        or birth.get("agent_id") != agent_name
        or birth.get("host") != host
    ):
        raise CodexActivityError("native compiled launch identity is unavailable")
    try:
        raw = birth["compiled_spec_json"]
        compiled = json.loads(raw)
        if (
            compiled.get("name") != agent_name
            or compiled.get("harness") != "codex"
            or compiled.get("runtime") != "tui"
        ):
            raise ValueError("wrong native launch")
        engine = compiled["engine_key"]
        model = compiled["claude"]["model"]
        if not all(
            isinstance(value, str) and value.strip() for value in (engine, model)
        ):
            raise ValueError("missing selected engine or model")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CodexActivityError(
            "native compiled launch identity is malformed"
        ) from exc
    return {
        "agent_id": agent_name,
        "spec_id": f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}",
        "host": host,
        "runtime": "tui",
        "harness": "codex",
        "engine": engine,
        "model": model,
    }


def _assert_monotonic(previous: dict | None, fields: dict) -> None:
    if (
        not isinstance(previous, dict)
        or previous.get("engine_incarnation_id") != fields["engine_incarnation_id"]
    ):
        return
    for key in (
        "codex_activity_at",
        "codex_event_seq",
        "turns_accepted",
        "turns_completed",
        "tools_started",
        "tools_completed",
    ):
        before = previous.get(key)
        if (
            isinstance(before, bool)
            or not isinstance(before, (int, float))
            or fields[key] < before
        ):
            raise CodexActivityError(
                "native activity publication regressed within its engine"
            )


def promote_codex_activity(
    state_dir: Path,
    agent_name: str,
    *,
    host: str,
    write_fn: Callable,
    instance_reader: Callable | None = None,
    birth_reader: Callable | None = None,
    now_fn: Callable[[], float] = time.time,
    proc_root: Path = Path("/proc"),
) -> CodexActivityObservation:
    """Publish only while canonical owner, root thread and file remain bound.

    The existing instance and birth-certificate package readers provide store
    authority. Native inherited environment values are not instance selectors.
    Missing/racing evidence raises and leaves the preceding heartbeat intact.
    """
    from .._runners._session_state import read_heartbeat, read_instance_id
    from .._state.authoritative_heartbeat import read_card_lease

    if instance_reader is None:
        from .._state.state_store_instances import read_instance as instance_reader
    if birth_reader is None:
        from .._state.state_store_incarnations import get_incarnation as birth_reader
    state_dir = Path(state_dir)
    instance_id = read_instance_id(state_dir)
    if not instance_id:
        raise CodexActivityError("native canonical instance marker is unavailable")
    record = instance_reader(instance_id)
    if not isinstance(record, dict):
        raise CodexActivityError("native canonical instance record is unavailable")
    binding = bind_codex_runtime(
        record,
        instance_id=instance_id,
        agent_name=agent_name,
        host=host,
        proc_root=proc_root,
    )
    identity = _launch_identity(
        birth_reader(instance_id),
        instance_id=instance_id,
        agent_name=agent_name,
        host=host,
    )
    observed = read_codex_activity(
        binding.rollout_path,
        expected_thread_id=binding.thread_id,
        observed_at=now_fn(),
        expected_file_identity=binding.rollout_identity,
    )
    turn_active = observed.turns_accepted > observed.turns_completed
    card_id, card_role = read_card_lease(state_dir, now=observed.observed_at)
    fields = {
        **identity,
        "session_id": binding.thread_id,
        "boot_id": binding.boot_id,
        "activity_source_id": _activity_source_id(binding),
        "activity_instance_id": binding.instance_id,
        "progress_at": observed.activity_at,
        "progress_seq": observed.event_seq,
        "engine_incarnation_id": f"{binding.boot_id}:{binding.thread_id}",
        "codex_thread_id": binding.thread_id,
        "codex_event_seq": observed.event_seq,
        "codex_activity_at": observed.activity_at,
        "turns_accepted": observed.turns_accepted,
        "turns_completed": observed.turns_completed,
        "tools_started": observed.tools_started,
        "tools_completed": observed.tools_completed,
        "tools_inflight": observed.tools_inflight,
        "codex_tools_inflight": list(observed.inflight_tool_ids),
        "last_event_type": observed.last_event_type,
        "last_turn_status": observed.last_turn_status,
        "last_error_code": observed.last_error_code,
        # Generic Claude sidecars/text do not measure native provider capacity.
        "capacity_status": "unknown",
        "capped": None,
        "current_phase": "blocked"
        if observed.last_turn_status == "error" and not turn_active
        else "",
        "card_id": card_id,
        "card_role": card_role,
    }
    previous = read_heartbeat(state_dir)
    _assert_monotonic(previous, fields)
    heartbeat_path = state_dir / "heartbeat.json"
    previous_bytes = heartbeat_path.read_bytes() if heartbeat_path.exists() else None

    def assert_owner():
        if read_instance_id(state_dir) != instance_id:
            raise CodexActivityError("native canonical instance marker changed")
        current_record = instance_reader(instance_id)
        if not isinstance(current_record, dict):
            raise CodexActivityError("native canonical instance record disappeared")
        assert_codex_binding_current(binding, current_record)

    assert_owner()
    busy = turn_active or observed.tools_inflight > 0
    try:
        write_fn(
            state_dir,
            pid=binding.native.pid,
            state="busy" if busy else "ready",
            ts=observed.observed_at,
            writer=WRITER_CODEX_ROLLOUT,
            name=agent_name,
            host=host,
            authoritative_fields=fields,
        )
        assert_owner()
    except Exception:
        # Retract only this observer's cache write; a concurrent successor owns
        # its own cache and must never be replaced with an older saved heartbeat.
        current = read_heartbeat(state_dir)
        if isinstance(current, dict) and (
            current.get("writer") == WRITER_CODEX_ROLLOUT
            and current.get("engine_incarnation_id") == fields["engine_incarnation_id"]
            and current.get("codex_event_seq") == observed.event_seq
            and current.get("ts") == observed.observed_at
        ):
            if previous_bytes is None:
                heartbeat_path.unlink(missing_ok=True)
            else:
                atomic_write_text(heartbeat_path, previous_bytes.decode("utf-8"))
        raise
    return observed
