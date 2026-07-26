"""Period-aware per-agent summaries for the local OpenAI usage ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .._usage_period import (
    parse_usage_timestamp,
    timestamp_in_period,
    usage_timestamp_iso,
)


def _zero_agent_spend() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unpriced_turns": 0,
        "first_observed_at": None,
        "last_observed_at": None,
        "retained_first_observed_at": None,
        "retained_last_observed_at": None,
        "timestamped_requests": 0,
        "untimestamped_requests": 0,
        "cumulative_requests": 0,
        "error": None,
    }


def summarize_agent_spend(
    agent: str,
    ledger: dict[str, Any] | None,
    *,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    """Summarize one agent, using exact events for a requested period."""
    out = _zero_agent_spend()
    agents = ledger.get("agents") if isinstance(ledger, dict) else None
    bucket = agents.get(agent) if isinstance(agents, dict) else None
    if not isinstance(bucket, dict):
        out["error"] = f"no OpenAI spend recorded for agent {agent!r}"
        return out
    out["cumulative_requests"] = int(bucket.get("requests") or 0)
    events_by_agent = ledger.get("agent_events")
    events = events_by_agent.get(agent) if isinstance(events_by_agent, dict) else None
    filtered = since is not None or until is not None
    if filtered and not isinstance(events, list):
        out["error"] = (
            "OpenAI usage predates timestamped per-agent accounting; "
            "period totals are unavailable"
        )
        return out
    if not filtered:
        out["requests"] = int(bucket.get("requests") or 0)
        out["input_tokens"] = int(bucket.get("input_tokens") or 0)
        out["output_tokens"] = int(bucket.get("output_tokens") or 0)
        out["estimated_cost_usd"] = round(float(bucket.get("spend_usd") or 0.0), 6)
        out["unpriced_turns"] = int(bucket.get("unpriced_turns") or 0)
    first: datetime | None = None
    last: datetime | None = None
    retained_first: datetime | None = None
    retained_last: datetime | None = None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        timestamp = parse_usage_timestamp(event.get("timestamp"))
        requests = int(event.get("requests") or 0)
        if timestamp is None:
            out["untimestamped_requests"] += requests
            continue
        out["timestamped_requests"] += requests
        if retained_first is None or timestamp < retained_first:
            retained_first = timestamp
        if retained_last is None or timestamp > retained_last:
            retained_last = timestamp
        if not timestamp_in_period(timestamp, since, until):
            continue
        if filtered:
            out["requests"] += requests
            out["input_tokens"] += int(event.get("input_tokens") or 0)
            out["output_tokens"] += int(event.get("output_tokens") or 0)
            out["estimated_cost_usd"] = round(
                out["estimated_cost_usd"] + float(event.get("spend_usd") or 0.0),
                6,
            )
            out["unpriced_turns"] += int(event.get("unpriced_turns") or 0)
        if first is None or timestamp < first:
            first = timestamp
        if last is None or timestamp > last:
            last = timestamp
    out["first_observed_at"] = usage_timestamp_iso(first)
    out["last_observed_at"] = usage_timestamp_iso(last)
    out["retained_first_observed_at"] = usage_timestamp_iso(retained_first)
    out["retained_last_observed_at"] = usage_timestamp_iso(retained_last)
    return out


__all__ = ["summarize_agent_spend"]
