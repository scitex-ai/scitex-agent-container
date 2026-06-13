"""Per-beat enrichment fields for ``_session_state.write_heartbeat``.

The session_jsonl movement signals live here (rather than alongside
``_heartbeat_usage_fields`` in ``_session_state``) so the
``_session_state`` module stays under the per-file line cap. Pure
read-side helpers — no subprocess, no side effects on the agent
state — so a heartbeat-loop crash here is impossible.

Operator-requested (``feedback_sac_heartbeat_observability``,
2026-06-13): embed "is this agent PRODUCING?" right next to the
liveness ts so a single heartbeat read answers both. Fields:

  * ``session_jsonl_bytes``       — current size of
    ``<state_dir>/session.jsonl`` (0 if absent).
  * ``session_jsonl_delta_bytes`` — bytes added since the previous
    heartbeat (positive = producing, 0 = idle though the beat
    fired). Computed against the PRIOR ``heartbeat.json``'s
    recorded value, clamped to >=0 so a session.jsonl rotate /
    truncate can't mislead with a negative.
  * ``seconds_since_last_beat``  — wall-clock gap to the prior beat.

CAVEAT — subagent/background work writes to a SUBAGENT jsonl, NOT
the main ``session.jsonl``. An active subagent + idle main session
therefore shows ``delta=0`` on the main beat (the false-idle hit
live on dev/todo flat-main while bg scanners ran). A follow-up PR
can opt into summing per-subagent jsonl deltas — deferred so the
operator can decide when the extra walk-I/O cost is worth the
precision.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["heartbeat_jsonl_fields"]


def heartbeat_jsonl_fields(state_dir: Path, now: float) -> dict:
    """Return the session-jsonl movement signals for a heartbeat record.

    NEVER raises — any read failure degrades to an empty / partial
    dict so the heartbeat loop can splat it onto the payload without
    risking a runner crash. Absent keys (rather than zero values)
    let the operator distinguish "first beat ever" / "missed prior
    beat" / "rotate happened" from a clean zero-delta idle.
    """
    out: dict = {}
    jsonl = state_dir / "session.jsonl"
    try:
        current_bytes = jsonl.stat().st_size if jsonl.is_file() else 0
    except OSError:
        return out
    out["session_jsonl_bytes"] = int(current_bytes)
    prior_path = state_dir / "heartbeat.json"
    try:
        prior_text = prior_path.read_text(encoding="utf-8")
    except OSError:
        return out
    try:
        prior = json.loads(prior_text) if prior_text else {}
    except json.JSONDecodeError:
        return out
    prior_ts = prior.get("ts")
    if isinstance(prior_ts, (int, float)) and prior_ts > 0:
        out["seconds_since_last_beat"] = round(max(0.0, now - prior_ts), 3)
    prior_bytes = prior.get("session_jsonl_bytes")
    if isinstance(prior_bytes, int) and prior_bytes >= 0:
        # Clamp >=0 — a session.jsonl rotate/truncate between beats
        # would otherwise produce a negative delta and mislead the
        # operator into thinking the agent destroyed work.
        out["session_jsonl_delta_bytes"] = max(0, int(current_bytes) - prior_bytes)
    return out
