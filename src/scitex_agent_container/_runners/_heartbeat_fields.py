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

Subagent / background-work productivity (false-idle gap CLOSED for
the layouts below):

  * ``subagent_jsonl_bytes``       — summed size of every candidate
    subagent jsonl found under ``state_dir`` (see below).
  * ``subagent_jsonl_delta_bytes`` — delta vs the PRIOR beat's
    ``subagent_jsonl_bytes``, clamped to >=0.

Both fields are ABSENT (not zero) when no candidate dir exists at
all — so the operator can distinguish "no subagent infrastructure
on this agent" from "subagents present but idle this beat".

Candidate layouts walked (``Path.glob``, stat-only, per-file
``OSError`` swallowed):

  * ``<state_dir>/subagents/*/session.jsonl`` — Claude Code sub-task
    sessions (one dir per spawned subagent).
  * ``<state_dir>/.tasks/*/output``           — sac background-task
    output files (dotfile layout).
  * ``<state_dir>/tasks/*/output``            — sac background-task
    output files (non-dot layout).

This closes the false-idle CAVEAT from PR #370: an active subagent
+ idle main session now surfaces as a positive
``subagent_jsonl_delta_bytes`` instead of a misleading zero on the
main ``session_jsonl_delta_bytes``.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["heartbeat_jsonl_fields"]


# Subagent / background-work jsonl candidate globs (relative to state_dir).
# Walked stat-only — never read. See module docstring for rationale.
_SUBAGENT_JSONL_GLOBS: tuple[str, ...] = (
    "subagents/*/session.jsonl",
    ".tasks/*/output",
    "tasks/*/output",
)


def _sum_subagent_jsonl_bytes(state_dir: Path) -> int | None:
    """Sum file sizes for every candidate subagent jsonl under ``state_dir``.

    Returns ``None`` when NO candidate path produced any match — the
    caller turns that into ABSENT keys so the operator can distinguish
    "no subagent infrastructure" from "subagents present but idle".
    Per-file ``OSError`` is swallowed (a racing rotate during the
    walk must not mask the rest of the productivity signal).
    """
    found = False
    total = 0
    for pattern in _SUBAGENT_JSONL_GLOBS:
        try:
            matches = list(state_dir.glob(pattern))
        except OSError:
            continue
        for path in matches:
            found = True
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total if found else None


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
    # Subagent walk runs even if prior heartbeat read fails below — the
    # current-size signal is independently useful (operator sees
    # subagent output now, even on first beat or after a corrupted
    # prior heartbeat.json).
    subagent_bytes = _sum_subagent_jsonl_bytes(state_dir)
    if subagent_bytes is not None:
        out["subagent_jsonl_bytes"] = int(subagent_bytes)
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
    if subagent_bytes is not None:
        prior_subagent = prior.get("subagent_jsonl_bytes")
        if isinstance(prior_subagent, int) and prior_subagent >= 0:
            # Clamp >=0 — subagent rotate / dir cleanup between beats
            # would otherwise surface as a misleading negative.
            out["subagent_jsonl_delta_bytes"] = max(
                0, int(subagent_bytes) - prior_subagent
            )
    return out
