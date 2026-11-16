"""``sac agents reconcile-turn-bridge`` bridge-only repair."""

from __future__ import annotations

import json

import click

from ..config import load_config
from ..config._host import resolve_hostname
from ..config._resolve import resolve_with_prefix
from ..runtimes._tui_turn_bridge_reconcile import reconcile_turn_bridge
from ._helpers import _json_flag


@click.command(name="reconcile-turn-bridge")
@click.argument("name")
@click.option(
    "--apply",
    "--yes",
    "-y",
    "apply",
    is_flag=True,
    default=False,
    help="Reload only the turn-bridge process. The TUI session is preserved.",
)
@click.option(
    "--dry-run", is_flag=True, help="Inspect existing live bridge state (the default)."
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def reconcile_turn_bridge_cmd(
    name: str, apply: bool, dry_run: bool, as_json: bool
) -> None:
    """Repair a live TUI's A2A bridge without restarting its agent session.

    Dry-run is the default. For ``a2a.port: auto``, the port is recovered from
    authoritative live instance/port state; this command never allocates a new
    endpoint for an existing TUI. Preview requires a live TUI and authoritative
    port state. Explicit --dry-run cannot be combined with approval.

    \b
    Examples:
      $ sac agents reconcile-turn-bridge worker-agent --dry-run --json
      $ sac agents reconcile-turn-bridge worker-agent --yes --json
    """
    if apply and dry_run:
        raise click.UsageError("--dry-run cannot be combined with --apply/--yes/-y")
    as_json = _json_flag(click.get_current_context(), as_json)
    try:
        config = load_config(resolve_with_prefix(name))
        result = reconcile_turn_bridge(
            config,
            apply=apply,
            current_host=resolve_hostname(),
        )
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"name": name, "status": "error", "error": str(exc)}))
        else:
            raise click.ClickException(str(exc)) from exc
        raise SystemExit(1) from exc
    payload = result.to_dict()
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    mode = "applied" if apply else "dry-run"
    click.echo(
        f"{result.status}: {result.name} turn bridge on port {result.port} "
        f"(source={result.source}, {mode}); TUI {result.session!r} preserved"
    )


def register(group: click.Group) -> None:
    group.add_command(reconcile_turn_bridge_cmd)


__all__ = ["reconcile_turn_bridge_cmd", "register"]
