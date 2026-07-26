"""Read token and current-session cost counters from Claude Code state.

The interactive TUI runtime does not pass through the SDK runner, so it
does not write ``runtime/<agent>/quota.json``.  Claude Code does, however,
persist per-response usage in ``~/.claude/projects/*/*.jsonl`` and its
status-line payload contains the provider-reported current-session cost.

This module only reads those provider-owned files.  Transcript tokens can
be converted to an API-equivalent estimate at Anthropic's published list
prices, but that estimate is deliberately separate from the current-session
provider-reported cost and is never described as a subscription charge.
Every failure is returned as data so an inspection command cannot break an
agent.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .._usage_period import parse_usage_timestamp, timestamp_in_period
from .claude_pricing import PRICE_SOURCE, PRICE_VERSION, estimate_message_cost_usd


def _zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "assistant_messages": 0,
        "transcript_files": 0,
        "first_observed_at": None,
        "last_observed_at": None,
        "retained_first_observed_at": None,
        "retained_last_observed_at": None,
        "timestamped_messages": 0,
        "untimestamped_messages": 0,
        "estimated_api_cost_usd": None,
        "cost_estimate_complete": False,
        "priced_messages": 0,
        "unpriced_messages": 0,
        "unpriced_models": [],
        "server_tool_requests": {},
        "model_costs_usd": {},
        "pricing_version": PRICE_VERSION,
        "pricing_source": PRICE_SOURCE,
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


def _server_tool_requests(usage: dict[str, Any]) -> dict[str, int]:
    raw = usage.get("server_tool_use")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


def read_claude_code_usage(
    home: Path | None,
    agent: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
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
            stream = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line_number, line in enumerate(stream, 1):
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
                timestamp = record.get("timestamp")
                timestamp_dt = parse_usage_timestamp(timestamp)
                if timestamp_dt is None:
                    out["untimestamped_messages"] += 1
                else:
                    out["timestamped_messages"] += 1
                    retained_first = out["retained_first_observed_at"]
                    retained_last = out["retained_last_observed_at"]
                    if retained_first is None or timestamp_dt < retained_first[0]:
                        out["retained_first_observed_at"] = (timestamp_dt, timestamp)
                    if retained_last is None or timestamp_dt > retained_last[0]:
                        out["retained_last_observed_at"] = (timestamp_dt, timestamp)
                if not timestamp_in_period(timestamp_dt, since, until):
                    continue
                out["assistant_messages"] += 1
                if timestamp_dt is not None:
                    first = out["first_observed_at"]
                    last = out["last_observed_at"]
                    if first is None or timestamp_dt < first[0]:
                        out["first_observed_at"] = (timestamp_dt, timestamp)
                    if last is None or timestamp_dt > last[0]:
                        out["last_observed_at"] = (timestamp_dt, timestamp)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ):
                    out[key] += _token(usage.get(key))
                billable_tokens = sum(
                    _token(usage.get(key))
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
                model = message.get("model")
                model = model if isinstance(model, str) and model else "<unknown>"
                estimated = estimate_message_cost_usd(
                    usage,
                    model,
                    timestamp=timestamp if isinstance(timestamp, str) else None,
                )
                if billable_tokens > 0 and estimated is None:
                    out["unpriced_messages"] += 1
                    if model not in out["unpriced_models"]:
                        out["unpriced_models"].append(model)
                elif billable_tokens > 0:
                    out["priced_messages"] += 1
                    current = float(out["estimated_api_cost_usd"] or 0.0)
                    out["estimated_api_cost_usd"] = current + estimated
                    model_costs = out["model_costs_usd"]
                    model_costs[model] = float(model_costs.get(model, 0.0)) + estimated
                for tool, count in _server_tool_requests(usage).items():
                    requests = out["server_tool_requests"]
                    requests[tool] = int(requests.get(tool, 0)) + count
    if out["estimated_api_cost_usd"] is not None:
        out["estimated_api_cost_usd"] = round(out["estimated_api_cost_usd"], 8)
    out["model_costs_usd"] = {
        model: round(cost, 8) for model, cost in sorted(out["model_costs_usd"].items())
    }
    out["unpriced_models"].sort()
    out["server_tool_requests"] = dict(sorted(out["server_tool_requests"].items()))
    for key in (
        "first_observed_at",
        "last_observed_at",
        "retained_first_observed_at",
        "retained_last_observed_at",
    ):
        stamped = out[key]
        out[key] = stamped[1] if stamped is not None else None
    out["cost_estimate_complete"] = (
        out["priced_messages"] > 0
        and out["unpriced_messages"] == 0
        and not out["server_tool_requests"]
    )
    if since is None and until is None:
        _read_statusline(Path(home), agent, out)
    if not paths and out["current_session_id"] is None:
        out["error"] = "no Claude Code usage state recorded yet"
    return out


__all__ = ["read_claude_code_usage"]
