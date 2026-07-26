"""Read token and current-session cost counters from Claude Code state.

The interactive TUI runtime does not pass through the SDK runner, so it
does not write ``runtime/<agent>/quota.json``.  Claude Code does, however,
persist per-response usage in ``~/.claude/projects/*/*.jsonl`` and its
status-line payload contains the provider-reported current-session cost.

This module only reads those provider-owned files.  It never estimates a
subscription charge from tokens, and every failure is returned as data so
an inspection command cannot break an agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "assistant_messages": 0,
        "transcript_files": 0,
        "current_session_cost_usd": None,
        "current_session_id": None,
        "error": None,
    }


def _token(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _read_statusline(home: Path, agent: str, out: dict[str, Any]) -> None:
    path = home / ".scitex" / "agent-container" / "statusline" / f"{agent}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    cost = payload.get("cost")
    value = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) >= 0.0
    ):
        out["current_session_cost_usd"] = round(float(value), 8)
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        out["current_session_id"] = session_id


def read_claude_code_usage(home: Path | None, agent: str) -> dict[str, Any]:
    """Aggregate unique assistant-message usage below one Claude Code home.

    UUID de-duplication prevents a branched session copied into multiple
    transcript files from being counted more than once within the agent.
    The cost is intentionally separate: Claude Code's status-line contract
    exposes only the current session's provider-reported total.
    """
    out = _zero_usage()
    if home is None:
        out["error"] = "Claude Code home could not be resolved"
        return out
    projects = Path(home) / ".claude" / "projects"
    # Include nested ``<session>/subagents/agent-*.jsonl`` records as well as
    # the main ``<session>.jsonl`` so delegated work is attributed to the
    # owning SAC agent.
    paths = sorted(projects.rglob("*.jsonl")) if projects.is_dir() else []
    seen: set[str] = set()
    for path in paths:
        out["transcript_files"] += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            identity = record.get("uuid")
            dedup_key = (
                identity
                if isinstance(identity, str) and identity
                else f"{path}:{line_number}"
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            out["assistant_messages"] += 1
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                out[key] += _token(usage.get(key))
    _read_statusline(Path(home), agent, out)
    if not paths and out["current_session_id"] is None:
        out["error"] = "no Claude Code usage state recorded yet"
    return out


__all__ = ["read_claude_code_usage"]
