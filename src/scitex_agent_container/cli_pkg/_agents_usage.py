"""Per-agent token and cost counters for ``sac agents usage``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.table import Table

from .._usage_period import (
    require_usage_timestamp,
    usage_timestamp_iso,
)
from ._agents_usage_period import build_usage_coverage, period_omissions
from ._helpers import agent_name_complete, console


def _token_total(tokens: dict) -> int:
    return sum(
        int(tokens.get(key, 0) or 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


def _resolve_claude_code_home(
    name: str, state_dir: Path | None, host_home: Path
) -> Path | None:
    """Resolve the host directory backing the TUI agent's container HOME."""
    candidates: list[Path] = []
    try:
        from .._state.registry import Registry
        from ..config import load_config
        from ..runtimes._to_home_overlay import resolve_overlay_upper_home

        entry = Registry().get(name) or {}
        config_path = entry.get("config")
        if isinstance(config_path, str) and Path(config_path).is_file():
            config = load_config(config_path, profile=entry.get("profile"))
            upper = resolve_overlay_upper_home(config)
            if upper is not None:
                candidates.append(upper)
    except Exception:  # stx-allow: fallback (reason: a stale registry/config must not hide readable local usage state)
        pass
    if state_dir is not None:
        candidates.append(state_dir / "home")
    candidates.append(
        host_home
        / ".scitex"
        / "agent-container"
        / "containers"
        / "overlays"
        / name
        / "upper"
        / "home"
        / "agent"
    )
    for candidate in candidates:
        has_transcripts = (candidate / ".claude" / "projects").is_dir()
        has_statusline = (
            candidate / ".scitex" / "agent-container" / "statusline" / f"{name}.json"
        ).is_file()
        if has_transcripts or has_statusline:
            return candidate
    return next((path for path in candidates if path.is_dir()), None)


def _combined_tokens(quota: dict, tui: dict, openai: dict) -> dict[str, int]:
    return {
        key: (
            int(quota.get(key, 0) or 0)
            + int(tui.get(key, 0) or 0)
            + int(openai.get(key, 0) or 0)
        )
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    }


def build_usage_payload(
    name: str,
    *,
    state_dir: Path | None = None,
    home: Path | None = None,
    agent_home: Path | None = None,
    usd_jpy_rate: float | None = None,
    fetch_fx: bool = False,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
) -> dict:
    """Build the stable token/cost payload for one agent."""
    from .._account.claude_code_usage import read_claude_code_usage
    from .._account.exchange_rates import resolve_usd_jpy_rate
    from .._account.openai_usage import read_agent_spend
    from .._lifecycle._session_movement import resolve_state_dir
    from .._runners._session_quota import read_quota, read_quota_period

    since_dt = require_usage_timestamp(since, "--since") if since is not None else None
    until_dt = require_usage_timestamp(until, "--until") if until is not None else None
    if since_dt is not None and until_dt is not None and since_dt >= until_dt:
        raise ValueError("--since must be earlier than --until")
    filtered = since_dt is not None or until_dt is not None

    resolved = state_dir if state_dir is not None else resolve_state_dir(name)
    if filtered:
        quota = read_quota_period(resolved, since=since_dt, until=until_dt)
    else:
        quota = read_quota(resolved)
        quota_history = read_quota_period(resolved)
        for key in (
            "first_observed_at",
            "last_observed_at",
            "retained_first_observed_at",
            "retained_last_observed_at",
            "timestamped_turns",
            "untimestamped_turns",
            "cumulative_turns",
            "history_matches_cumulative_totals",
            "error",
        ):
            quota[key] = quota_history[key]
    host_home = Path(home) if home is not None else Path.home()
    tui_home = (
        Path(agent_home)
        if agent_home is not None
        else _resolve_claude_code_home(name, resolved, host_home)
    )
    tui = read_claude_code_usage(
        tui_home,
        name,
        since=since_dt,
        until=until_dt,
    )
    openai = read_agent_spend(
        name,
        home=host_home,
        since=since_dt,
        until=until_dt,
    )
    raw_tokens = _combined_tokens(quota, tui, openai)
    tokens = {
        "input": raw_tokens["input_tokens"],
        "output": raw_tokens["output_tokens"],
        "cache_creation_input": raw_tokens["cache_creation_input_tokens"],
        "cache_read_input": raw_tokens["cache_read_input_tokens"],
    }
    tokens["total"] = _token_total(raw_tokens)
    provider_available = int(quota.get("costed_turns", 0) or 0) > 0
    openai_available = openai["error"] is None
    claude_estimate = tui["estimated_api_cost_usd"]
    fx = {
        "rate": None,
        "rate_date": None,
        "source": None,
        "from_cache": False,
        "stale": False,
        "error": None,
    }
    if claude_estimate is not None and (fetch_fx or usd_jpy_rate is not None):
        fx = resolve_usd_jpy_rate(home=host_home, override=usd_jpy_rate)
    claude_jpy = (
        round(float(claude_estimate) * float(fx["rate"]), 2)
        if claude_estimate is not None and fx["rate"] is not None
        else None
    )
    coverage = build_usage_coverage(
        quota,
        tui,
        openai,
        since=since_dt,
        until=until_dt,
    )
    return {
        "agent": name,
        "period": {
            "since": usage_timestamp_iso(since_dt),
            "until": usage_timestamp_iso(until_dt),
            "until_exclusive": True,
        },
        "tokens": tokens,
        "activity": {
            "sdk_turns": int(quota.get("turns", 0) or 0),
            "claude_code_assistant_messages": tui["assistant_messages"],
            "openai_requests": openai["requests"],
        },
        "coverage": coverage,
        "cost": {
            "currency": "USD",
            "sdk_provider_reported_usd": (
                round(float(quota.get("cost_usd", 0.0) or 0.0), 8)
                if provider_available
                else None
            ),
            "sdk_costed_turns": int(quota.get("costed_turns", 0) or 0),
            "sdk_unpriced_turns": int(quota.get("uncosted_turns", 0) or 0),
            "claude_code_current_session_usd": tui["current_session_cost_usd"],
            "claude_api_estimated_usd": claude_estimate,
            "claude_api_estimated_jpy": claude_jpy,
            "claude_api_estimate_complete": tui["cost_estimate_complete"],
            "claude_api_priced_messages": tui["priced_messages"],
            "claude_api_unpriced_messages": tui["unpriced_messages"],
            "claude_api_unpriced_models": tui["unpriced_models"],
            "claude_api_model_costs_usd": tui["model_costs_usd"],
            "claude_api_excluded_server_tool_requests": tui["server_tool_requests"],
            "openai_estimated_usd": (
                openai["estimated_cost_usd"] if openai_available else None
            ),
            "openai_unpriced_turns": openai["unpriced_turns"],
            "usd_jpy": fx,
        },
        "sources": {
            "sdk_quota": (
                str(resolved / "quota.json")
                if resolved is not None and (resolved / "quota.json").is_file()
                else None
            ),
            "claude_code_home": str(tui_home) if tui_home else None,
            "claude_code_transcript_files": tui["transcript_files"],
            "claude_code_session_id": tui["current_session_id"],
            "claude_code_error": tui["error"],
            "claude_pricing_version": tui["pricing_version"],
            "claude_pricing": tui["pricing_source"],
            "openai_estimate": (
                "local list-price ledger" if openai_available else None
            ),
            "openai_error": openai["error"],
        },
        "note": (
            "List-price estimates are API-equivalent usage metrics, not Claude "
            "Pro/Max subscription fees or an invoice. They exclude discounts, "
            "taxes, and separately priced server tools."
        ),
    }


def _fmt_tokens(value: int) -> str:
    return f"{value:,}"


def _render_human(payload: dict) -> None:
    tokens = payload["tokens"]
    activity = payload["activity"]
    coverage = payload["coverage"]
    cost = payload["cost"]
    period = payload["period"]
    table = Table(title=f"Agent usage: {payload['agent']}")
    table.add_column("Counter", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("SDK turns", _fmt_tokens(activity["sdk_turns"]))
    table.add_row(
        "Claude Code assistant messages",
        _fmt_tokens(activity["claude_code_assistant_messages"]),
    )
    table.add_row("Input tokens", _fmt_tokens(tokens["input"]))
    table.add_row("Output tokens", _fmt_tokens(tokens["output"]))
    table.add_row("Cache creation tokens", _fmt_tokens(tokens["cache_creation_input"]))
    table.add_row("Cache read tokens", _fmt_tokens(tokens["cache_read_input"]))
    table.add_row("Total tokens", _fmt_tokens(tokens["total"]))
    table.add_row(
        "SDK provider-reported cost (USD)",
        _fmt_cost(cost["sdk_provider_reported_usd"]),
    )
    table.add_row(
        "Claude Code current-session cost (USD)",
        _fmt_cost(cost["claude_code_current_session_usd"]),
    )
    estimate_label = "Claude API-equivalent estimate (USD)"
    if (
        cost["claude_api_estimated_usd"] is not None
        and not cost["claude_api_estimate_complete"]
    ):
        estimate_label += " [partial]"
    table.add_row(
        estimate_label,
        _fmt_usd(cost["claude_api_estimated_usd"]),
    )
    table.add_row(
        "Claude API-equivalent estimate (JPY)",
        _fmt_jpy(cost["claude_api_estimated_jpy"]),
    )
    table.add_row(
        "USD/JPY reference rate",
        _fmt_fx(cost["usd_jpy"]),
    )
    table.add_row(
        "OpenAI estimated cost (USD)",
        _fmt_cost(cost["openai_estimated_usd"]),
    )
    if period["since"] is not None or period["until"] is not None:
        table.add_row(
            "Period start (UTC, inclusive)",
            period["since"] or "unbounded",
        )
        table.add_row(
            "Period end (UTC, exclusive)",
            period["until"] or "unbounded",
        )
    table.add_row(
        "First observed (UTC)",
        _fmt_timestamp(coverage["first_observed_at"]),
    )
    table.add_row(
        "Last observed (UTC)",
        _fmt_timestamp(coverage["last_observed_at"]),
    )
    console.print(table)
    console.print(f"[dim]Coverage: {coverage['basis']}.[/dim]")
    omissions = period_omissions(payload)
    if omissions:
        console.print(
            "[yellow]Excluded legacy usage without period timestamps: "
            + "; ".join(omissions)
            + ".[/yellow]"
        )
    console.print(f"[dim]{payload['note']}[/dim]")


def _fmt_cost(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:.6f}"


def _fmt_usd(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:,.2f}"


def _fmt_jpy(value: float | None) -> str:
    if value is None:
        return "unavailable"
    from decimal import ROUND_HALF_UP, Decimal

    rounded = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"¥{rounded:,}"


def _fmt_fx(value: dict) -> str:
    rate = value.get("rate")
    if rate is None:
        return "unavailable"
    suffix = f" ({value['rate_date']})" if value.get("rate_date") else ""
    if value.get("stale"):
        suffix += " [stale]"
    return f"{float(rate):.6f}{suffix}"


def _fmt_timestamp(value: str | None) -> str:
    return value or "unknown"


@click.command("usage")
@click.argument("name", shell_complete=agent_name_complete)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "--last",
    type=str,
    default=None,
    help="Count only the last duration from now (for example 30m, 24h, 7d).",
)
@click.option(
    "--since",
    type=str,
    default=None,
    help="Inclusive ISO-8601 period start (UTC when no offset is supplied).",
)
@click.option(
    "--until",
    type=str,
    default=None,
    help="Exclusive ISO-8601 period end (UTC when no offset is supplied).",
)
@click.option(
    "--usd-jpy-rate",
    type=click.FloatRange(min=0.000001, min_open=False),
    default=None,
    help=(
        "Override JPY per USD. Defaults to SAC_USD_JPY_RATE, then the cached "
        "ECB reference rate."
    ),
)
def agents_usage(
    name: str,
    as_json: bool,
    last: str | None,
    since: str | None,
    until: str | None,
    usd_jpy_rate: float | None,
) -> None:
    """Show accumulated or period-filtered agent token usage and costs.

    Reads SDK quota state and Claude Code TUI transcripts. Costs remain
    separated by scope: SDK accumulated, Claude Code current-session,
    Claude API-equivalent list-price estimate in USD/JPY, and OpenAI local
    list-price estimate. None is a subscription fee or invoice.

    \b
    Example:
      $ sac agents usage sales
      $ sac agents usage sales --last 24h
      $ sac agents usage sales --since 2026-07-26T00:00:00Z
      $ sac agents usage sales --since 2026-07-01 --until 2026-08-01
      $ sac agents usage sales --json
      $ sac agents usage sales --usd-jpy-rate 160
    """
    if last is not None and (since is not None or until is not None):
        raise click.UsageError("--last cannot be combined with --since or --until")
    try:
        since_dt = (
            require_usage_timestamp(since, "--since") if since is not None else None
        )
        until_dt = (
            require_usage_timestamp(until, "--until") if until is not None else None
        )
        if last is not None:
            from .._state.recall import parse_duration

            duration = parse_duration(last)
            until_dt = datetime.now(timezone.utc)
            since_dt = until_dt - duration
        if since_dt is not None and until_dt is not None and since_dt >= until_dt:
            raise ValueError("--since must be earlier than --until")
        payload = build_usage_payload(
            name,
            usd_jpy_rate=usd_jpy_rate,
            fetch_fx=True,
            since=since_dt,
            until=until_dt,
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    _render_human(payload)


__all__ = ["agents_usage", "build_usage_payload"]
