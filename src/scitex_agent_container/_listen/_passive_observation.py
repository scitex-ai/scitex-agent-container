"""Passive fleet batch evidence fenced by current kernel/source metadata."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .._runners._session_state import read_heartbeat, read_instance_id, state_dir_for
from .._state.authoritative_heartbeat import (
    AuthoritativeHeartbeatError,
    validate_heartbeat,
)
from ..runtimes._activity_source_identity import (
    SOURCE_KIND,
    activity_source_id,
)
from ..runtimes._codex_activity import CodexActivityError
from ..runtimes._codex_activity_binding import bind_codex_runtime
from ..runtimes._codex_activity_projection import _launch_identity, activity_harness
from ._handshake_observation import _time
from ._handshake_snapshot import (
    LEDGER_PAGE_SIZE,
    handshake_snapshot,
    read_handshake_page,
)
from ._observation_contract import (
    AgentObservation,
    HandshakeSnapshot,
    LedgerPage,
    ObservationAuthority,
    ObservationFrame,
    RuntimeObservation,
)

_PRODUCER_EPOCH = uuid.uuid4().hex
_COUNTERS: tuple[str, ...] = ()


class UnsupportedObservationAdapter(CodexActivityError):
    """A known launch lacks a qualified passive source binding adapter."""


def capture_observation_authority(
    name, host, active, birth, state_dir, *, proc_root=Path("/proc")
):
    """Use existing owned native adapter; no proof read, RPC or credentials.

    This adapter reads bounded primary identity headers, never native tool
    output. Other adapters need their own qualified binding and remain unknown.
    """
    instance_id = read_instance_id(state_dir)
    candidates = [
        record
        for record in active
        if record.get("id") == instance_id
        and record.get("name") == name
        and record.get("host") == host
    ]
    if len(candidates) != 1:
        raise CodexActivityError("canonical observation owner is unavailable")
    if not isinstance(birth, dict):
        raise CodexActivityError("canonical observation birth is unavailable")
    selected = activity_harness(
        state_dir,
        name,
        host=host,
        declared_harness="",
        birth_reader=lambda _: birth,
    )
    if selected != "codex":
        raise UnsupportedObservationAdapter("passive source capability is unknown")
    _launch_identity(birth, instance_id=instance_id, agent_name=name, host=host)
    binding = bind_codex_runtime(
        candidates[0],
        instance_id=instance_id,
        agent_name=name,
        host=host,
        proc_root=proc_root,
    )
    target = {
        "agent": name,
        "host": host,
        "instance_id": instance_id,
        "boot_id": binding.boot_id,
        "session_id": binding.thread_id,
    }
    return ObservationAuthority(
        **target,
        source={
            "kind": SOURCE_KIND,
            "identity": activity_source_id(target, binding.rollout_identity),
        },
    )


def _threshold(value):
    valid = _time(value)
    return 0.0 if type(value) in {int, float} and value == 0 else valid


def runtime_observation(heartbeat, authority, *, now, progress_stale_s=None):
    """Publish measured clocks separately from work and process liveness."""
    _ = progress_stale_s  # accepted for call-site compatibility only
    unknown = RuntimeObservation()
    if (
        not isinstance(heartbeat, dict)
        or heartbeat.get("activity_source_id") != authority.source.identity
    ):
        return unknown
    resident = heartbeat.get("authoritative_heartbeat")
    if not isinstance(resident, dict):
        return unknown
    try:
        resident = validate_heartbeat(
            resident,
            expected_agent=authority.agent,
            expected_host=authority.host,
            now=now,
        )
    except (AuthoritativeHeartbeatError, TypeError, OverflowError):
        return unknown
    if (
        heartbeat.get("activity_instance_id") != authority.instance_id
        or resident["boot_id"] != authority.boot_id
        or resident["session_id"] != authority.session_id
        or any(
            heartbeat.get(key) != resident[key]
            for key in (
                "agent_id",
                "spec_id",
                "host",
                "runtime",
                "harness",
                "engine",
                "model",
                "session_id",
                "boot_id",
            )
        )
        or heartbeat.get("ts") != resident["observed_at"]
        or _time(now) is None
        or resident["observed_at"] > now
    ):
        return unknown
    observed = _time(resident["observed_at"])
    if observed is None:
        return unknown
    deadline = resident["lease_expires_at"]
    result = RuntimeObservation(
        authority=authority,
        observed_at=observed,
        age_s=now - observed,
        heartbeat_lease_s=deadline - observed,
        lease_expires_at=deadline,
        lease_remaining_s=deadline - now,
        lease_expired=now > deadline,
    )
    progress = _time(resident["progress_at"])
    session_delta = resident.get("session_jsonl_delta_bytes")
    subagent_delta = resident.get("subagent_jsonl_delta_bytes")
    for delta in (session_delta, subagent_delta):
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            continue
        if delta > 0 and progress is not None and progress <= now:
            result.session_jsonl_delta_bytes = float(session_delta) if isinstance(session_delta, (int, float)) and not isinstance(session_delta, bool) else None
            result.subagent_jsonl_delta_bytes = float(subagent_delta) if isinstance(subagent_delta, (int, float)) and not isinstance(subagent_delta, bool) else None
            result.progress_at = progress
            result.progress_age_s = now - progress
            break
    if now > deadline:
        return result
    # Binary verdict inputs only: deltas + nonce pair. A positive delta
    # inside the lease marks the resident observed; resident_state reads
    # the binary WORKING-or-DEAD vocabulary (CCT 4276/4299/4309).
    session_delta = resident.get("session_jsonl_delta_bytes")
    subagent_delta = resident.get("subagent_jsonl_delta_bytes")
    deltas = [
        float(value)
        for value in (session_delta, subagent_delta)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not any(value > 0 for value in deltas):
        result.resident_state = "dead"
        return result
    challenge = resident.get("nonce_challenge")
    if isinstance(challenge, str) and len(challenge) == 16 and challenge.isdigit():
        from .._state.authoritative_heartbeat import nonce_echo_confirms

        if not nonce_echo_confirms(resident):
            result.resident_state = "dead"
            return result
    result.state = "observed"
    result.resident_state = "working"
    return result


def annotate_observation_rows(
    rows,
    *,
    active,
    births,
    local_host,
    capture_fn=None,
    heartbeat_reader=None,
    ledger_reader=None,
    state_dir_fn=None,
    clock=None,
    order_fn=None,
    producer_epoch=_PRODUCER_EPOCH,
    progress_stale_s=None,
):
    """One scoped ledger page; double source capture refuses mixed frames.

    No challenges, ACKs, exchange advances, provider probes or runtime commands
    occur here. A resource-page truncation is explicitly incomplete/unknown.
    """
    capture = capture_fn or capture_observation_authority
    read_beat = heartbeat_reader or read_heartbeat
    read_page = ledger_reader or read_handshake_page
    directory = state_dir_fn or state_dir_for
    current_clock = clock or time.time
    started_at = _time(current_clock())
    captured, directories, reasons = {}, {}, {}
    for row in rows:
        name = row.get("name")
        if (
            not isinstance(name, str)
            or not name
            or row.get("host") not in (None, "", local_host)
        ):
            continue
        try:
            directories[name] = directory(name)
            captured[name] = capture(
                name, local_host, active, births.get(name), directories[name]
            )
        except UnsupportedObservationAdapter:
            reasons[name] = "capability_unknown"
        except (CodexActivityError, OSError, TypeError, ValueError):
            continue
    page = LedgerPage(limit=LEDGER_PAGE_SIZE, returned=0, complete=True)
    ledger, ledger_available = [], True
    if captured:
        try:
            ledger, page = read_page(tuple(captured))
        except Exception:  # stx-allow: fallback (a failed passive ledger boundary publishes unknown, never admission or death)
            ledger_available = False
    heartbeats = {}
    for name in captured:
        try:
            heartbeats[name] = read_beat(directories[name])
        except (OSError, TypeError, ValueError):
            heartbeats[name] = None
    for name, authority in tuple(captured.items()):
        try:
            if (
                capture(name, local_host, active, births.get(name), directories[name])
                != authority
            ):
                del captured[name]
        except (CodexActivityError, OSError, TypeError, ValueError):
            del captured[name]
    now = _time(current_clock())
    valid_clock = now is not None and started_at is not None and now >= started_at
    frame = ObservationFrame(
        producer_epoch=producer_epoch,
        order=(order_fn or time.monotonic_ns)(),
        observed_at=now if valid_clock else None,
        clock="valid" if valid_clock else "uncertain",
    )
    result = []
    for row in rows:
        name = row.get("name")
        local_row = row.get("host") in (None, "", local_host)
        authority = captured.get(name) if local_row else None
        if authority is None:
            handshake = HandshakeSnapshot(
                state="unknown",
                reason=(
                    reasons.get(name, "authority_unknown")
                    if local_row
                    else "authority_unknown"
                ),
                page=page,
            )
        elif not valid_clock:
            handshake = HandshakeSnapshot(
                state="unknown", reason="clock_uncertain", page=page
            )
        elif not ledger_available:
            handshake = HandshakeSnapshot(
                state="unknown", reason="ledger_unavailable", page=page
            )
        else:
            handshake = handshake_snapshot(authority, ledger, page, now=now)
        runtime = (
            runtime_observation(
                heartbeats.get(name),
                authority,
                now=now,
                progress_stale_s=progress_stale_s,
            )
            if authority is not None and valid_clock
            else RuntimeObservation()
        )
        observation = AgentObservation(
            authority=authority, frame=frame, handshake=handshake, runtime=runtime
        )
        result.append({**row, "observation": observation.model_dump(mode="json")})
    return result
