"""Bound fresh restarts to confirmed quota events from official API probes."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from .._reconcile._budget import Budget


def restart_capped_agents(document, *, state_path: Path, running_names, restart, now=None):
    """Restart running consumers once per cap event; unknown never triggers it.

    The lifecycle callback must preflight both roles before replacing a process.
    A restart cannot replenish quota; exhausted credentials retain their reset
    metadata and the successor must select a usable declared credential.
    """
    stamp = time.time() if now is None else now
    observed = document.get("checked_at")
    if not isinstance(observed, (int, float)) or not 0 <= stamp - observed <= 300:
        return [{"status": "unknown", "reason": "quota evidence is stale or missing"}]
    candidates = {}
    for alias, row in document.get("slots", {}).items():
        if row.get("verdict") != "exhausted":
            continue
        for name in row.get("agents", ()):
            if name in running_names:
                candidates.setdefault(name, []).append((alias, row.get("reason"), row.get("capped_windows", [])))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    with state_path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        history = state.setdefault("history", {})
        seen = state.setdefault("seen", {})
        # A recovered allowance re-arms a later, independently observed cap.
        state["seen"] = seen = {name: value for name, value in seen.items() if name in candidates}
        budget = Budget(history=history)

        def persist():
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
            os.replace(temporary, state_path)

        for name, caps in sorted(candidates.items()):
            fingerprint = hashlib.sha256(json.dumps(sorted(caps), sort_keys=True).encode()).hexdigest()
            if seen.get(name) == fingerprint:
                reports.append({"agent": name, "status": "already-restarted"})
                continue
            admission = budget.check(name, stamp)
            if not admission.allowed:
                reports.append({"agent": name, "status": admission.reason})
                continue
            budget.record(name, stamp)
            persist()  # A crashed callback must not lose its restart budget.
            try:
                completed = restart(name, session_override="fresh", drain_timeout_s=30) is not False
            except Exception as exc:
                reports.append({"agent": name, "status": "failed", "error_type": type(exc).__name__})
                continue
            if completed:
                seen[name] = fingerprint
            reports.append({"agent": name, "status": "restarted-fresh" if completed else "refused"})
        persist()
    return reports
