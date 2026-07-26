"""Coverage metadata and omission notices for period usage reports."""

from __future__ import annotations

from datetime import datetime

from .._usage_period import parse_usage_timestamp, usage_timestamp_iso


def build_usage_coverage(
    quota: dict,
    tui: dict,
    openai: dict,
    *,
    since: datetime | None,
    until: datetime | None,
) -> dict:
    """Describe the selected records and retained timestamp coverage."""
    observed = [
        parsed
        for parsed in (
            parse_usage_timestamp(quota.get("first_observed_at")),
            parse_usage_timestamp(tui.get("first_observed_at")),
            parse_usage_timestamp(openai.get("first_observed_at")),
        )
        if parsed is not None
    ]
    observed_last = [
        parsed
        for parsed in (
            parse_usage_timestamp(quota.get("last_observed_at")),
            parse_usage_timestamp(tui.get("last_observed_at")),
            parse_usage_timestamp(openai.get("last_observed_at")),
        )
        if parsed is not None
    ]
    filtered = since is not None or until is not None
    basis = (
        "retained timestamped local usage records in the requested UTC "
        "half-open interval; untimestamped and cumulative-only legacy "
        "records are excluded"
        if filtered
        else (
            "all retained local usage state; observed timestamps cover "
            "timestamped records and may not bound legacy cumulative usage"
        )
    )
    return {
        "first_observed_at": (usage_timestamp_iso(min(observed)) if observed else None),
        "last_observed_at": (
            usage_timestamp_iso(max(observed_last)) if observed_last else None
        ),
        "basis": basis,
        "sources": {
            "sdk": {
                "first_observed_at": quota.get("first_observed_at"),
                "last_observed_at": quota.get("last_observed_at"),
                "retained_first_observed_at": quota.get("retained_first_observed_at"),
                "retained_last_observed_at": quota.get("retained_last_observed_at"),
                "history_matches_cumulative_totals": quota.get(
                    "history_matches_cumulative_totals"
                ),
                "timestamped_turns": quota.get("timestamped_turns"),
                "untimestamped_turns": quota.get("untimestamped_turns"),
                "cumulative_turns": quota.get("cumulative_turns"),
                "error": quota.get("error"),
            },
            "claude_code": {
                "first_observed_at": tui["first_observed_at"],
                "last_observed_at": tui["last_observed_at"],
                "retained_first_observed_at": tui["retained_first_observed_at"],
                "retained_last_observed_at": tui["retained_last_observed_at"],
                "timestamped_messages": tui["timestamped_messages"],
                "untimestamped_messages": tui["untimestamped_messages"],
                "error": tui["error"],
            },
            "openai": {
                "first_observed_at": openai["first_observed_at"],
                "last_observed_at": openai["last_observed_at"],
                "retained_first_observed_at": openai["retained_first_observed_at"],
                "retained_last_observed_at": openai["retained_last_observed_at"],
                "timestamped_requests": openai["timestamped_requests"],
                "untimestamped_requests": openai["untimestamped_requests"],
                "cumulative_requests": openai["cumulative_requests"],
                "error": openai["error"],
            },
        },
    }


def period_omissions(payload: dict) -> list[str]:
    """Describe cumulative records that cannot enter a filtered total."""
    period = payload["period"]
    if period["since"] is None and period["until"] is None:
        return []
    sources = payload["coverage"]["sources"]
    sdk = sources["sdk"]
    openai = sources["openai"]
    claude = sources["claude_code"]
    omissions: list[str] = []
    cumulative_turns = int(sdk.get("cumulative_turns") or 0)
    timestamped_turns = int(sdk.get("timestamped_turns") or 0)
    if cumulative_turns > timestamped_turns:
        omissions.append(f"{cumulative_turns - timestamped_turns:,} SDK turn(s)")
    cumulative_requests = int(openai.get("cumulative_requests") or 0)
    timestamped_requests = int(openai.get("timestamped_requests") or 0)
    if cumulative_requests > timestamped_requests:
        omissions.append(
            f"{cumulative_requests - timestamped_requests:,} OpenAI request(s)"
        )
    untimestamped_messages = int(claude.get("untimestamped_messages") or 0)
    if untimestamped_messages:
        omissions.append(f"{untimestamped_messages:,} Claude Code assistant message(s)")
    return omissions


__all__ = ["build_usage_coverage", "period_omissions"]
