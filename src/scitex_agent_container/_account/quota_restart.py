"""Bound fresh restarts to confirmed quota events from official API probes."""

import fcntl
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from .._reconcile._budget import Budget
from .provider_usage_projection import timestamp


def _event_id(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()


def _cap_events(alias, row, now):
    """Only a current, identified quota window establishes a restart event."""
    events = {}
    windows = row.get("capped_windows")
    if not isinstance(alias, str) or not alias or not isinstance(windows, list):
        return events
    for window in windows:
        if not isinstance(window, dict):
            continue
        name = window.get("name")
        remaining = window.get("remaining")
        reset = timestamp(window.get("resetAt"))
        if (
            not isinstance(name, str)
            or not name
            or window.get("state") != "known"
            or type(remaining) not in (int, float)
            or remaining != 0
            or reset is None
            or datetime.fromisoformat(reset).timestamp() <= now
        ):
            continue
        event = {"slot": alias, "window": name, "resetAt": reset}
        events[_event_id(event)] = event
    return events


def _read_event_ledger(state):
    """Refuse malformed recovery memory rather than treating it as empty."""
    ledger = state.setdefault("cap_events", {})
    if not isinstance(ledger, dict):
        raise ValueError("quota cap-event ledger is malformed")
    for events in ledger.values():
        if not isinstance(events, dict):
            raise ValueError("quota cap-event ledger entries are malformed")
        for identity, event in events.items():
            if (
                not isinstance(event, dict)
                or set(event) != {"slot", "window", "resetAt"}
                or any(
                    not isinstance(value, str) or not value for value in event.values()
                )
                or timestamp(event["resetAt"]) != event["resetAt"]
                or _event_id(event) != identity
            ):
                raise ValueError("quota cap-event identity is malformed")
    return ledger


def _clear_recovered_events(slots, ledger, seen):
    """Only positive recovery of that same slot re-arms its handled events."""
    for name, events in ledger.items():
        recovered = [
            identity
            for identity, event in events.items()
            if slots.get(event["slot"], {}).get("verdict") == "alive"
        ]
        for identity in recovered:
            del events[identity]
        if recovered and not events:
            seen.pop(name, None)


def restart_capped_agents(
    document, *, state_path: Path, running_names, restart, now=None
):
    """Deduplicate successful fresh restarts per identified cap event.

    This configured-slot policy does not prove a client's in-memory active key.
    The lifecycle callback must preflight both roles before replacing a process.
    Missing reset identities stay unknown; unknown observations never re-arm a
    previously handled cap. A callback exception after a physical restart leaves
    an uncertain outcome: this is not a strict at-most-once guarantee. Failed
    callbacks retain their spent budget and existing retry eligibility.
    Fresh-session goal continuity is a separate contract.
    """
    stamp = time.time() if now is None else now
    observed = document.get("checked_at")
    if not isinstance(observed, (int, float)) or not 0 <= stamp - observed <= 300:
        return [{"status": "unknown", "reason": "quota evidence is stale or missing"}]
    candidates = {}
    events_by_agent = {}
    for alias, row in document.get("slots", {}).items():
        if row.get("verdict") != "exhausted":
            continue
        for name in row.get("agents", ()):
            if name in running_names:
                candidates.setdefault(name, []).append(
                    (alias, row.get("reason"), row.get("capped_windows", []))
                )
                events_by_agent.setdefault(name, {}).update(
                    _cap_events(alias, row, stamp)
                )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    with state_path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        history = state.setdefault("history", {})
        seen = state.setdefault("seen", {})
        if not isinstance(seen, dict) or any(
            not isinstance(v, str) for v in seen.values()
        ):
            raise ValueError("legacy quota cap-event memory is malformed")
        ledger = _read_event_ledger(state)
        _clear_recovered_events(document.get("slots", {}), ledger, seen)
        budget = Budget(history=history, pass_cap=2)

        def persist():
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
            os.replace(temporary, state_path)

        for name, caps in sorted(candidates.items()):
            events = events_by_agent[name]
            if not events:
                reports.append({"agent": name, "status": "unknown-cap-identity"})
                continue
            fingerprint = hashlib.sha256(
                json.dumps(sorted(caps), sort_keys=True).encode()
            ).hexdigest()
            handled = ledger.get(name)
            if handled is None and name in seen:
                if seen[name] != fingerprint:
                    reports.append(
                        {"agent": name, "status": "legacy-cap-identity-unknown"}
                    )
                    continue
                # Exact legacy evidence can be adopted without repeating its action.
                ledger[name] = handled = dict(events)
            if events.keys() <= (handled or {}).keys():
                reports.append({"agent": name, "status": "already-restarted"})
                continue
            admission = budget.check(name, stamp)
            if not admission.allowed:
                reports.append({"agent": name, "status": admission.reason})
                continue
            budget.record(name, stamp)
            persist()  # A crashed callback must not lose its restart budget.
            from .._lifecycle._hermes_restart_preflight import reset_probe_cache

            reset_probe_cache()  # A new authoritative cap supersedes recent successes.
            try:
                completed = (
                    restart(name, session_override="fresh", drain_timeout_s=30)
                    is not False
                )
            except Exception as exc:
                reports.append(
                    {
                        "agent": name,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            if completed:
                seen[name] = fingerprint  # Preserve the legacy field for older readers.
                ledger.setdefault(name, {}).update(events)
            reports.append(
                {"agent": name, "status": "restarted-fresh" if completed else "refused"}
            )
        persist()
    return reports
