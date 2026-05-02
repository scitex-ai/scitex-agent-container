"""``sac recall <jsonl>`` — summarize / read back a Claude Code session jsonl.

Used after a host crash to reconstruct what the dead agent was doing
without paying the cost of a full ``--continue`` resume.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from ..recall import (
    collect_stats,
    filter_entries,
    format_entry,
    format_stats,
    iter_entries,
    parse_duration,
)


def _resolve_jsonl(arg: str) -> Path:
    """Accept a path, a session id, or '<agent-name>:<session-id>'."""
    p = Path(arg).expanduser()
    if p.exists():
        return p
    # Bare session id: search ~/.claude/projects/*/<id>.jsonl
    candidates = list(Path.home().glob(f".claude/projects/*/{arg}.jsonl"))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise click.ClickException(
            f"Ambiguous session id {arg!r} matches {len(candidates)} files"
        )
    raise click.ClickException(f"jsonl not found: {arg}")


@click.command()
@click.argument("jsonl", type=str)
@click.option(
    "--stats",
    "stats_only",
    is_flag=True,
    default=False,
    help="Print stats summary only (no per-message body).",
)
@click.option(
    "--last",
    "last",
    type=str,
    default=None,
    help="Limit to the final window of the transcript "
    "(e.g. '8h', '30m', '1.5d'). Anchored on the transcript's last "
    "timestamp, not wallclock now.",
)
@click.option(
    "--since",
    "since",
    type=str,
    default=None,
    help="ISO-8601 timestamp; show entries strictly after this.",
)
@click.option(
    "--until",
    "until",
    type=str,
    default=None,
    help="ISO-8601 timestamp; show entries strictly before this.",
)
@click.option(
    "--role",
    "role",
    type=click.Choice(["user", "assistant", "system", "all"]),
    default=None,
    help="Filter by message role. 'all' includes infra types too.",
)
@click.option(
    "--contains",
    "contains",
    type=str,
    default=None,
    help="Case-insensitive substring filter on message text.",
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=None,
    help="Cap to the last N matching entries (after filtering).",
)
@click.option(
    "--include-thinking",
    is_flag=True,
    default=False,
    help="Include [thinking] parts (off by default — usually noise).",
)
@click.option(
    "--no-tool-results",
    "no_tool_results",
    is_flag=True,
    default=False,
    help="Drop synthetic 'user' records that are really tool_result "
    "callbacks. Useful when --role user should mean 'human prompts only'.",
)
@click.option(
    "--body-limit",
    "body_limit",
    type=int,
    default=600,
    help="Truncate each printed message body to this many chars (0 = no limit).",
)
def recall(
    jsonl: str,
    stats_only: bool,
    last: str | None,
    since: str | None,
    until: str | None,
    role: str | None,
    contains: str | None,
    limit: int | None,
    include_thinking: bool,
    no_tool_results: bool,
    body_limit: int,
) -> None:
    """Summarize a Claude Code session jsonl.

    JSONL can be a full path or a bare session id (auto-resolved against
    ``~/.claude/projects/*/``).

    \b
    Example:
      $ sac recall <session-id>
      $ sac recall head-ywata-note-win:<session-id>
      $ sac recall <session-id> --since 1h --role user
    """
    path = _resolve_jsonl(jsonl)
    stats = collect_stats(path)
    click.echo("# stats")
    click.echo(format_stats(stats))
    if stats_only:
        return

    last_td = parse_duration(last) if last else None
    since_dt = _parse_iso(since) if since else None
    until_dt = _parse_iso(until) if until else None

    # Anchor "last" on the transcript's last timestamp, not wallclock now.
    reference_now = stats.last_ts if last_td else None

    entries = list(
        filter_entries(
            iter_entries(path),
            last=last_td,
            since=since_dt,
            until=until_dt,
            role=role,
            contains=contains,
            include_thinking=include_thinking,
            include_tool_results=not no_tool_results,
            reference_now=reference_now,
        )
    )

    if limit is not None and limit > 0 and len(entries) > limit:
        entries = entries[-limit:]

    click.echo(f"\n# entries ({len(entries)})")
    bl = body_limit if body_limit and body_limit > 0 else None
    for e in entries:
        click.echo("")
        click.echo(format_entry(e, body_limit=bl))


def _parse_iso(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (
        ValueError
    ) as exc:  # stx-allow: fallback (reason: type coercion or format mismatch)
        raise click.ClickException(f"invalid ISO timestamp: {s!r} ({exc})")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(recall.main(standalone_mode=True))
