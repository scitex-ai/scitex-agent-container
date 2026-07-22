"""``sac subagent`` noun group — Claude Code Agent-tool subagent monitoring.

Type 2 subagents — the ones the Claude Code Agent tool spawns inside a
single Claude Code session — as distinct from sac's own apptainer
``agents`` group (Type 1).

Scope: pure state inspection. Classification (running / stale / dead /
completed) is deliberately left to the consumer (any
orchestrator); this surface returns the same facts that the
``subagent_get_state`` MCP tool returns.
"""

from __future__ import annotations

import json as json_mod

import click

from .._mcp._tools import _subagent
from ._helpers import _json_flag, console


@click.group(
    "subagent",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def subagent_group() -> None:
    """Claude Code Agent-tool subagent monitoring (Type 2).

    \b
    Examples:
      $ sac subagent get-state
      $ sac subagent get-state --json
      $ sac subagent get-state --agent-id abc123
    """


@subagent_group.command("get-state")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Restrict to one subagent whose filename matches agent-<ID>.jsonl.",
)
@click.option(
    "--project-path",
    "project_path",
    default=None,
    help="Absolute project path; defaults to the current working directory.",
)
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Claude Code session UUID; if omitted, every session is scanned.",
)
@click.option(
    "--projects-root",
    "projects_root",
    default=None,
    type=click.Path(file_okay=False, path_type=str),
    help=(
        "Override the ~/.claude/projects root (escape hatch for sandboxed "
        "shells; tests pass a tmp_path here)."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON array.")
@click.pass_context
def get_state(
    ctx: click.Context,
    agent_id: str | None,
    project_path: str | None,
    session_id: str | None,
    projects_root: str | None,
    as_json: bool,
) -> None:
    """Pure state data for every matching Claude Code subagent.

    Walks ``~/.claude/projects/<project_hash>/<session>/subagents/agent-*.jsonl``
    and prints one row per transcript. Returns the same dicts as the
    ``subagent_get_state`` MCP tool — no classification, just facts.

    \b
    Example:
      $ sac subagent get-state
      $ sac subagent get-state --json
      $ sac subagent get-state --agent-id abc123 --json
    """
    states = _subagent.subagent_get_state(
        agent_id=agent_id,
        project_path=project_path,
        session_id=session_id,
        projects_root=projects_root,
    )
    if _json_flag(ctx, as_json):
        click.echo(json_mod.dumps(states, indent=2, default=str))
        return
    _render_table(states)


def _render_table(states: list[dict]) -> None:
    """Pretty-print state dicts as a rich table.

    Imported lazily inside the function so ``sac subagent --help`` and
    ``sac subagent get-state --json`` don't pay for the rich.table
    import on the cold-start path.
    """
    from rich.table import Table

    if not states:
        console.print("[dim](no Claude Code subagents found for this project)[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("last_tool")
    table.add_column("size_bytes", justify="right")
    table.add_column("mtime_iso")
    table.add_column("done", justify="center")
    table.add_column("description", overflow="fold")
    for s in states:
        table.add_row(
            str(s.get("id", "")),
            str(s.get("last_tool") or "—"),
            str(s.get("size_bytes", 0)),
            str(s.get("mtime_iso", "")),
            "yes" if s.get("has_completed_marker") else "",
            (s.get("description") or "")[:80],
        )
    console.print(table)


__all__ = ["subagent_group"]
