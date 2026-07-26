"""Per-agent token and cost counters for ``sac agents usage``."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

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


def _combined_tokens(quota: dict, tui: dict) -> dict[str, int]:
    return {
        key: int(quota.get(key, 0) or 0) + int(tui.get(key, 0) or 0)
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
) -> dict:
    """Build the stable token/cost payload for one agent."""
    from .._account.claude_code_usage import read_claude_code_usage
    from .._account.openai_usage import read_agent_spend
    from .._lifecycle._session_movement import resolve_state_dir
    from .._runners._session_quota import read_quota

    resolved = state_dir if state_dir is not None else resolve_state_dir(name)
    quota = read_quota(resolved)
    host_home = Path(home) if home is not None else Path.home()
    tui_home = (
        Path(agent_home)
        if agent_home is not None
        else _resolve_claude_code_home(name, resolved, host_home)
    )
    tui = read_claude_code_usage(tui_home, name)
    openai = read_agent_spend(name, home=host_home)
    raw_tokens = _combined_tokens(quota, tui)
    tokens = {
        "input": raw_tokens["input_tokens"],
        "output": raw_tokens["output_tokens"],
        "cache_creation_input": raw_tokens["cache_creation_input_tokens"],
        "cache_read_input": raw_tokens["cache_read_input_tokens"],
    }
    tokens["total"] = _token_total(raw_tokens)
    provider_available = int(quota.get("costed_turns", 0) or 0) > 0
    openai_available = openai["error"] is None
    return {
        "agent": name,
        "tokens": tokens,
        "activity": {
            "sdk_turns": int(quota.get("turns", 0) or 0),
            "claude_code_assistant_messages": tui["assistant_messages"],
            "openai_requests": openai["requests"],
        },
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
            "openai_estimated_usd": (
                openai["estimated_cost_usd"] if openai_available else None
            ),
            "openai_unpriced_turns": openai["unpriced_turns"],
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
            "openai_estimate": (
                "local list-price ledger" if openai_available else None
            ),
            "openai_error": openai["error"],
        },
        "note": (
            "Provider-reported and list-price-estimated costs are usage metrics; "
            "they are not necessarily subscription charges or an invoice."
        ),
    }


def _fmt_tokens(value: int) -> str:
    return f"{value:,}"


def _render_human(payload: dict) -> None:
    tokens = payload["tokens"]
    activity = payload["activity"]
    cost = payload["cost"]
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
        "SDK provider-reported cost",
        _fmt_cost(cost["sdk_provider_reported_usd"]),
    )
    table.add_row(
        "Claude Code current-session cost",
        _fmt_cost(cost["claude_code_current_session_usd"]),
    )
    table.add_row(
        "OpenAI estimated cost",
        _fmt_cost(cost["openai_estimated_usd"]),
    )
    console.print(table)
    console.print(f"[dim]{payload['note']}[/dim]")


def _fmt_cost(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:.6f}"


@click.command("usage")
@click.argument("name", shell_complete=agent_name_complete)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def agents_usage(name: str, as_json: bool) -> None:
    """Show an agent's accumulated token usage and USD cost counters.

    Reads SDK quota state and Claude Code TUI transcripts. Costs remain
    separated by scope: SDK accumulated, Claude Code current-session,
    and OpenAI local list-price estimate. None is a fee or invoice.

    \b
    Example:
      $ sac agents usage sales
      $ sac agents usage sales --json
    """
    payload = build_usage_payload(name)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    _render_human(payload)


__all__ = ["agents_usage", "build_usage_payload"]
