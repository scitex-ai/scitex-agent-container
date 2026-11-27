"""``sac agents migrate-spec-uuids`` — move host-only uuid stamps into state.

SAC < 0.40 wrote ``uuid:`` into specs on start. Where that line exists only
in the working tree (absent from the spec's git ``HEAD``), this command
records the uuid in the agent's runtime state file (``<state_dir>/spec_uuid``,
the file the start path reads for a spec without ``uuid``) and removes the
line, so the agent keeps its identity and the spec checkout goes clean.
Authored (committed) uuids are untouched. See
:mod:`..config._spec_uuid_migrate` for the per-spec rules.

Exit codes: ``0`` every spec is migrated, persisted or needs nothing;
``1`` at least one spec was REFUSED (conflicting identities) or errored.
Running agents are not touched — only the state file and the spec file.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._helpers import _json_flag

_EXIT_OK = 0
_EXIT_REFUSED = 1


def _default_agents_dir() -> Path:
    from .._reconcile._pass import fleet_agents_dir

    return fleet_agents_dir()


def _render(results, *, dry_run: bool, agents_dir: Path) -> None:
    mode = "dry-run (nothing written)" if dry_run else "apply"
    click.echo(f"sac agents migrate-spec-uuids  {mode}  — {agents_dir}")
    if not results:
        click.echo("  no specs found")
    for r in results:
        line = f"  {r.action.upper():10s} {r.agent}"
        if r.uuid:
            line += f"  uuid={r.uuid}"
        if r.state_path and r.action in ("migrated", "persisted", "refused"):
            line += f"  state={r.state_path}"
        if r.line_removed:
            line += "  (line " + ("would be " if dry_run else "") + "removed)"
        click.echo(line)
        if r.reason and r.action not in ("no-uuid",):
            click.echo(f"      {r.reason}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "0 specs"
    click.echo(f"summary: {summary}")


@click.command(name="migrate-spec-uuids")
@click.argument("name", required=False)
@click.option(
    "--agents-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Specs root (default: the fleet registry, "
    "$SCITEX_AGENT_CONTAINER_AGENTS_DIR or ~/.scitex/agent-container/agents).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the plan; write nothing.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def migrate_spec_uuids_cmd(
    ctx: click.Context,
    name: "str | None",
    agents_dir: "Path | None",
    dry_run: bool,
    as_json: bool,
) -> None:
    """Move working-tree-only ``spec.uuid`` lines into runtime state.

    For each spec (or just NAME): a ``uuid:`` line absent from the spec's
    git HEAD is a SAC < 0.40 start stamp. Its value is written to the
    agent's ``<state_dir>/spec_uuid`` and the line is removed. Committed
    uuids are left alone; a state file holding a DIFFERENT uuid is refused.
    Outside git the uuid is persisted to state and the line is kept.

    \b
    Example:
      $ sac agents migrate-spec-uuids --dry-run
      $ sac agents migrate-spec-uuids scitex-dev --dry-run
      $ sac agents migrate-spec-uuids
      $ sac agents migrate-spec-uuids --agents-dir ~/.dotfiles/src/.scitex/agent-container/agents --json
    """
    from ..config._spec_uuid_migrate import migrate_spec_uuids

    root = (agents_dir or _default_agents_dir()).expanduser()
    if not root.is_dir():
        raise click.UsageError(f"agents dir does not exist: {root}")
    try:
        results = migrate_spec_uuids(root, name=name, dry_run=dry_run)
    except FileNotFoundError as exc:
        raise click.UsageError(str(exc)) from exc
    failed = any(r.action in ("refused", "error") for r in results)
    code = _EXIT_REFUSED if failed else _EXIT_OK
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "agents_dir": str(root),
                    "dry_run": dry_run,
                    "results": [r.to_dict() for r in results],
                    "exit_code": code,
                },
                indent=2,
            )
        )
    else:
        _render(results, dry_run=dry_run, agents_dir=root)
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``migrate-spec-uuids`` to the parent ``agents`` Click group."""
    agent_group.add_command(migrate_spec_uuids_cmd)


__all__ = ["migrate_spec_uuids_cmd", "register"]
