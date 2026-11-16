"""``sac config`` — harness-neutral agent config (``.agents/``), ADR-0033.

One source, two layers (user ``~/.scitex/agent-container/.agents``, project
``<git-root>/.scitex/agent-container/.agents``; project wins), rendered into
each harness's native files. Claude Code is the first adapter.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

HARNESSES = ("claude",)
SCOPES = ("user", "project")


def _claude_user_dir() -> Path:
    # Honour Claude Code's own override so a throwaway config dir can be used.
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def _resolve(scope: str, source: str | None, target: str | None):
    from .._neutral_config import (
        find_project_root,
        load_layer,
        project_layer_root,
        user_layer_root,
    )

    if scope == "user":
        src = Path(source) if source else user_layer_root()
        dest = Path(target) if target else _claude_user_dir()
    else:
        root = find_project_root()
        if root is None and not (source and target):
            raise click.UsageError(
                "--scope project needs a git repository (or --source and --target)"
            )
        src = Path(source) if source else project_layer_root(root)
        dest = Path(target) if target else root / ".claude"
    if not src.is_dir():
        raise click.UsageError(f"no neutral layer at {src}")
    return load_layer(src), dest


def _plan(harness: str, scope: str, source: str | None, target: str | None):
    from .._neutral_config import NeutralConfigError, plan_claude

    layer, dest = _resolve(scope, source, target)
    try:
        actions, notes = plan_claude(layer, dest, scope)
    except NeutralConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    return layer, dest, actions, notes


_harness_opt = click.option(
    "--harness",
    type=click.Choice(HARNESSES),
    default="claude",
    show_default=True,
    help="Native format to render (codex, gemini, hermes, opencode: planned, ADR-0033).",
)
_scope_opt = click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="user",
    show_default=True,
    help="user: ~/.scitex/agent-container/.agents -> ~/.claude; project: <git-root>/.scitex/agent-container/.agents -> <git-root>/.claude.",
)
_source_opt = click.option(
    "--source",
    type=click.Path(file_okay=False),
    help="Neutral layer dir (default: by scope).",
)
_target_opt = click.option(
    "--target",
    type=click.Path(file_okay=False),
    help="Native dir to render into (default: by scope).",
)


@click.group(name="config")
def config_group() -> None:
    """Neutral agent config (.agents/): show, deploy to native, check drift, import.

    Example:
      $ sac config deploy --harness claude --scope user --dry-run
    """


@config_group.command("show")
@_scope_opt
@_source_opt
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def show(scope: str, source: str | None, as_json: bool) -> None:
    """Summarise a neutral layer (what deploy would render from).

    Example:
      $ sac config show --scope user
    """
    layer, _ = _resolve(scope, source, None)
    summary = {
        "root": str(layer.root),
        "AGENTS.md": bool(layer.agents_md),
        "docs": sorted(layer.docs),
        "skills": len(layer.skills),
        "subagents": len(layer.subagents),
        "commands": len(layer.commands),
        "hooks": len(layer.hooks),
        "mcp_servers": sorted(layer.mcp_servers),
        "settings": sorted(layer.settings),
    }
    if as_json:
        click.echo(json.dumps(summary, indent=2))
        return
    for key, value in summary.items():
        click.echo(f"{key:12} {value}")


@config_group.command("deploy")
@_harness_opt
@_scope_opt
@_source_opt
@_target_opt
@click.option("--dry-run", is_flag=True, help="Show the plan; change nothing.")
def deploy(
    harness: str, scope: str, source: str | None, target: str | None, dry_run: bool
) -> None:
    """Render the neutral layer into the harness's native files.

    Real files in the way are moved to <target>/.sac-backup/<stamp>/, never
    deleted; live state (credentials, transcripts) is never touched.

    Example:
      $ sac config deploy --harness claude --scope user
    """
    from .._neutral_config import apply, check

    _, dest, actions, notes = _plan(harness, scope, source, target)
    results = check(dest, actions) if dry_run else apply(dest, actions)
    for result in results:
        click.echo(result.line())
    for note in notes:
        click.echo(f"[note] {note}")
    changed = sum(r.state != "ok" for r in results)
    verb = "would change" if dry_run else "changed"
    click.echo(f"{harness} {scope}: {verb} {changed} of {len(results)} into {dest}")


@config_group.command("check")
@_harness_opt
@_scope_opt
@_source_opt
@_target_opt
def check_cmd(harness: str, scope: str, source: str | None, target: str | None) -> None:
    """Report drift between the neutral layer and native files (exit 1 on drift).

    Example:
      $ sac config check --harness claude --scope user
    """
    from .._neutral_config import check

    _, dest, actions, _ = _plan(harness, scope, source, target)
    drift = [r for r in check(dest, actions) if r.state != "ok"]
    for result in drift:
        click.echo(result.line())
    click.echo(
        f"{harness} {scope}: {'in sync' if not drift else f'{len(drift)} drifted'} ({dest})"
    )
    sys.exit(1 if drift else 0)


@config_group.command("import")
@click.option(
    "--from",
    "from_harness",
    type=click.Choice(HARNESSES),
    default="claude",
    show_default=True,
)
@click.option(
    "--claude-dir",
    type=click.Path(file_okay=False),
    help="Claude config dir (default: ~/.claude).",
)
@click.option(
    "--to",
    "dest",
    type=click.Path(file_okay=False),
    required=True,
    help="Neutral layer dir to create.",
)
@click.option(
    "--copy-trees",
    is_flag=True,
    help="Also copy hooks/, agents/ (as subagents/), commands/, skills/.",
)
def import_cmd(
    from_harness: str, claude_dir: str | None, dest: str, copy_trees: bool
) -> None:
    """Create a neutral layer from an existing native config (migration).

    Example:
      $ sac config import --from claude --to /tmp/agents-layer
    """
    from .._neutral_config import import_claude

    src = Path(claude_dir) if claude_dir else _claude_user_dir()
    try:
        report = import_claude(src, Path(dest), copy_trees=copy_trees)
    except (FileExistsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for line in report:
        click.echo(line)


__all__ = ["config_group"]
