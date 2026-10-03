"""Canonical instance observations, isolated from lifecycle and provider calls.

The two-second budget bounds this enrichment batch, including owned-worker
cleanup. Existing store reads and fleet SSH transport have their own budgets.
No pane text, transcript text, credentials or credential locators are returned.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import secrets
import time
from pathlib import Path

from ._agent_observation_io import process_environment


def unknown(reason="not-observed"):
    return {
        "schema_version": 1,
        "process": "unknown",
        "work": "unknown",
        "work_reason": reason,
        "selection": {"state": "unknown"},
        "versions": {"state": "unknown"},
        "capacity": {"state": "unknown"},
    }


def assert_instance(record, *, proc_root=Path("/proc")):
    """Require the exact canonical kernel incarnation, never Registry.pid."""
    from ..._runners._tmux._process_group import _identity

    fields = ("pid", "process_start_time", "process_uid")
    if any(type(record.get(key)) is not int or record[key] < 0 for key in fields):
        raise ValueError("canonical-kernel-identity-unavailable")
    if not record.get("id") or record.get("ended_at") or record.get("remote"):
        raise ValueError("canonical-local-instance-required")
    identity = _identity(record["pid"], proc_root=proc_root)
    if identity is None or identity.state == "Z":
        raise ValueError("canonical-process-unavailable")
    if (
        identity.start_time != record["process_start_time"]
        or identity.uid != record["process_uid"]
        or identity.uid != os.getuid()
        or not record.get("control_group")
        or identity.control_group != record["control_group"]
    ):
        raise ValueError("canonical-kernel-identity-changed")
    return identity


def _selected_process(record, snapshot, *, proc_root):
    """Resolve an owned selected runtime even when its activity is incomplete."""
    harness = str(snapshot.get("harness") or "").lower()
    if harness == "codex":
        from ...runtimes._codex_activity_binding import bind_codex_runtime

        binding = bind_codex_runtime(
            record,
            instance_id=record["id"],
            agent_name=record["name"],
            host=record["host"],
            proc_root=proc_root,
        )
        return proc_root / str(binding.native.pid), binding
    elif harness == "hermes":
        from ..._lifecycle._session_movement import resolve_state_dir
        from ..._runners._hermes_owned_session import _gateway_evidence

        state_dir = resolve_state_dir(record["name"])
        if state_dir is None:
            raise ValueError("hermes-owned-projection-unavailable")
        gateway, gateway_stat = _gateway_evidence(state_dir, host_bind=True)
        # The descriptor's launcher must belong to this canonical owned tree.
        from ..._runners._tmux._process_group import capture_owned_process_tree

        tree = capture_owned_process_tree(record["pid"], proc_root=proc_root)
        if not {gateway["owner_pid"], gateway["pid"]}.issubset(
            {item.pid for item in tree}
        ):
            raise ValueError("hermes-canonical-owner-mismatch")
        return proc_root / str(gateway["pid"]), (state_dir, gateway, gateway_stat)
    raise ValueError("typed-runtime-observer-unavailable")


def _work(record, snapshot, selected, *, now):
    """Use the existing typed, owned native readers; incomplete work abstains."""
    harness = str(snapshot.get("harness") or "").lower()
    if harness == "codex":
        from ...runtimes._codex_activity import read_codex_activity
        from ...runtimes._codex_activity_binding import assert_codex_binding_current

        binding = selected
        observed = read_codex_activity(
            binding.rollout_path,
            expected_thread_id=binding.thread_id,
            observed_at=now,
            expected_file_identity=binding.rollout_identity,
        )
        assert_codex_binding_current(binding, record)
        state = (
            "active"
            if observed.tools_inflight
            or observed.turns_accepted > observed.turns_completed
            else (
                "blocked"
                if observed.last_turn_status == "error"
                else ("idle" if observed.turns_accepted > 0 else "unknown")
            )
        )
    elif harness == "hermes":
        from ..._runners._hermes_owned_session import _gateway_evidence
        from ...runtimes._hermes_heartbeat_projection import (
            read_hermes_heartbeat_projection,
        )

        state_dir, gateway, gateway_stat = selected
        observed = read_hermes_heartbeat_projection(
            state_dir, record["name"], previous=None, now_fn=lambda: now
        )
        if _gateway_evidence(state_dir, host_bind=True) != (gateway, gateway_stat):
            raise ValueError("hermes-gateway-changed")
        state = (
            "active"
            if observed.state == "busy"
            else ("blocked" if observed.last_turn_status == "error" else "idle")
        )
    else:
        raise ValueError("typed-work-observer-unavailable")
    counters = {
        key: getattr(observed, key)
        for key in (
            "turns_accepted",
            "turns_completed",
            "tools_started",
            "tools_completed",
            "tools_inflight",
        )
    }
    return {
        "state": state,
        "source": "owned-" + harness + "-events",
        "observed_at": observed.observed_at,
        "activity_at": observed.activity_at,
        "counters": counters,
    }


def observe_instance(
    record, snapshot, detail, salt, *, proc_root=Path("/proc"), now=None
):
    """Observe one already selected canonical row; no account/provider activation."""
    before = assert_instance(record, proc_root=proc_root)
    observed_at = time.time() if now is None else now
    result = unknown("typed-work-observation-unavailable")
    result.update(
        process="alive",
        instance_id=record["id"],
        observed_at=observed_at,
        evidence_source="canonical-instance-and-kernel",
    )
    process = selected = None
    try:
        process, selected = _selected_process(record, snapshot, proc_root=proc_root)
        work = _work(record, snapshot, selected, now=observed_at)
        result.update(
            work=work["state"],
            work_reason=(
                "typed-turn-lifecycle-unavailable"
                if work["state"] == "unknown"
                else None
            ),
            activity=work,
        )
    except (OSError, ValueError, RuntimeError):
        pass
    if detail and process is not None:
        from ._agent_observation_selection import cached_usage, selected_account

        try:
            environment = process_environment(process)
            selection = selected_account(snapshot, process, environment, salt)
            result["selection"] = {
                "state": "observed",
                **selection,
                "group_scope": "owning-host-observation",
                "capacity": "unknown",
            }
            result["capacity"] = cached_usage(snapshot)
        except (OSError, ValueError, TypeError, KeyError):
            environment = {}
            result["selection"] = {
                "state": "unknown",
                "reason": "selected-account-unavailable",
            }
        if detail >= 2:
            from ._agent_observation_versions import observe_versions

            try:
                result["versions"] = observe_versions(process, environment)
            except (OSError, ValueError):
                result["versions"] = {
                    "state": "unknown",
                    "reason": "namespace-metadata-unavailable",
                }
    after = assert_instance(record, proc_root=proc_root)

    def stable(identity):
        return (
            identity.pid,
            identity.parent_pid,
            identity.process_group,
            identity.session,
            identity.start_time,
            identity.uid,
            identity.control_group,
        )

    if stable(before) != stable(after):
        raise ValueError("canonical-kernel-identity-changed")
    if selected is not None:
        current_process, current_selected = _selected_process(
            record, snapshot, proc_root=proc_root
        )
        if (current_process, current_selected) != (process, selected):
            raise ValueError("selected-runtime-changed")
    return result


def observe_status(name, detail=0):
    """Use the owning host's canonical readers for the named CLI/HTTP view."""
    from ..._state.state_store import _resolve_host, list_active_instances
    from ..._state.state_store_incarnations import get_incarnations

    host = _resolve_host(None)
    instances = list_active_instances(host=host)
    selected = [record for record in instances if record.get("name") == name]
    births = {}
    if len(selected) == 1:
        instance_id = selected[0].get("id")
        records = get_incarnations((instance_id,)) if instance_id else {}
        if instance_id in records:
            births[name] = records[instance_id]
    return enrich_rows([{"name": name}], instances, host, detail=detail, births=births)[
        0
    ]["observation"]


def _worker(send, record, snapshot, detail, salt, observer):
    try:
        send.send(observer(record, snapshot, detail, salt))
    except (
        Exception
    ):  # stx-allow: fallback (row-local observation failure; no private exception text)
        send.send(unknown("canonical-observation-unavailable"))
    finally:
        send.close()


def enrich_rows(
    rows,
    instances,
    host,
    *,
    detail=0,
    births=None,
    budget=2.0,
    max_workers=4,
    observer=observe_instance,
):
    """Keep every row, including ambiguous owners and timed-out observations."""
    context = multiprocessing.get_context("spawn")
    salt = secrets.token_bytes(32)
    deadline = time.monotonic() + max(0, min(2.0, budget) - 0.2)
    outputs = [dict(row, observation=unknown()) for row in rows]
    pending = []
    canonical_indices = set()
    for index, row in enumerate(rows):
        row_host = row.get("host")
        if row_host not in {None, "", "local", host}:
            outputs[index]["observation"] = unknown("owning-host-observation-required")
            continue
        candidates = [
            record
            for record in instances
            if record.get("name") == row.get("name")
            and record.get("host") == host
            and not record.get("remote")
            and not record.get("ended_at")
        ]
        if candidates:
            canonical_indices.add(index)
            # An ambiguous/unverified current row is not proof of a stopped agent.
            outputs[index]["status"] = "unknown"
            outputs[index]["liveness_unknown"] = True
            outputs[index]["observation"]["canonical_instance_present"] = True
        if len(candidates) != 1:
            outputs[index]["observation"] = unknown(
                "canonical-instance-ambiguous"
                if candidates
                else "canonical-instance-unavailable"
            )
            continue
        record = candidates[0]
        if any(
            other.get("pid") == record.get("pid")
            and other.get("id") != record.get("id")
            and other.get("host") == host
            and not other.get("ended_at")
            and not other.get("remote")
            for other in instances
        ):
            outputs[index]["observation"] = unknown(
                "canonical-process-shared-by-instances"
            )
            continue
        birth = (births or {}).get(row.get("name"), {})
        snapshot = birth.get("compiled_spec_json")
        try:
            snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        except ValueError:
            snapshot = None
        if birth.get("incarnation_id") != record.get("id") or not isinstance(
            snapshot, dict
        ):
            snapshot = {}
        pending.append((index, record, snapshot))
    active = {}
    try:
        while (pending or active) and time.monotonic() < deadline:
            while (
                pending
                and len(active) < max(1, min(4, max_workers))
                and time.monotonic() < deadline
            ):
                index, record, snapshot = pending.pop(0)
                receive, send = context.Pipe(duplex=False)
                child = context.Process(
                    target=_worker,
                    args=(send, record, snapshot, min(3, detail), salt, observer),
                    daemon=True,
                )
                try:
                    child.start()
                except (OSError, RuntimeError):
                    receive.close()
                    send.close()
                    outputs[index]["observation"] = unknown("observation-worker-failed")
                    continue
                send.close()
                active[index] = child, receive
            for index, (child, receive) in list(active.items()):
                if receive.poll():
                    try:
                        observation = receive.recv()
                        observation["canonical_instance_present"] = True
                        outputs[index]["observation"] = observation
                        observation["observation_scope"] = {
                            "host": host,
                            "batch_id": hashlib.sha256(salt).hexdigest()[:16],
                        }
                        if observation.get("process") == "alive":
                            outputs[index]["status"] = (
                                "auth-failed"
                                if rows[index].get("auth_failed")
                                else "running"
                            )
                            outputs[index]["liveness_unknown"] = False
                    except (EOFError, OSError):
                        outputs[index]["observation"] = unknown(
                            "observation-worker-failed"
                        )
                    receive.close()
                    child.join(0.01)
                    if not child.is_alive():
                        del active[index]
                    else:
                        child.terminate()
                        child.join(0.02)
                        if child.is_alive():
                            child.kill()
                            child.join(0.02)
                        del active[index]
                elif not child.is_alive():
                    receive.close()
                    child.join(0.01)
                    outputs[index]["observation"] = unknown("observation-worker-failed")
                    del active[index]
            if active:
                time.sleep(0.005)
    finally:
        for index, (child, receive) in active.items():
            receive.close()
            if child.is_alive():
                child.terminate()
            child.join(0.02)
            if child.is_alive():
                child.kill()
                child.join(0.02)
            if outputs[index]["observation"]["process"] == "unknown":
                outputs[index]["observation"] = unknown("observation-deadline")
        for index, _, _ in pending:
            outputs[index]["observation"] = unknown("observation-deadline")
        for index in canonical_indices:
            outputs[index]["observation"]["canonical_instance_present"] = True
    return outputs
