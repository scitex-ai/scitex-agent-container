"""``sac host validate`` — check config.yaml, including whether it EXISTS.

Split out of ``host_group.py`` to keep that orchestrator under the per-file line
cap; attached onto the ``host`` group at import time via
:func:`register_validate_command` (same pattern as ``_account_list_cmd.py``).

INCIDENT 2026-07-30: this command reported ``{"source": "/home/agent/.scitex/
agent-container/config.yaml", "errors": []}`` while that file did not exist —
the operator's real 4-peer config lives under their own home, and a container's
``$HOME`` is per-container. Every ``sac host probe`` failed with "peer is not
defined in config.yaml" while validate called the configuration clean, so a peer
building on this rail could not tell a misconfiguration from a working one.

The cause is that ``load()`` maps a MISSING file onto the same defaults as a
present one, and an absent config has no schema to violate — so the single
condition that actually breaks multi-host was the one condition this check could
not fail on. ``sac host list`` already printed "(no config.yaml found)" for the
same state; that inconsistency between two readers of one fact is what made the
gap visible.
"""

from __future__ import annotations

import json

import click

from .._state.host_config import load
from .._state.host_config_diagnose import config_state_problems
from ._helpers import _json_flag, console


@click.command("validate")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_validate(ctx: click.Context, as_json: bool) -> None:
    """Check config.yaml for misconfiguration; exit non-zero on errors.

    \b
    Example:
      $ sac host validate
      $ sac host validate --json
    """
    cfg = load()
    # The state diagnosis runs FIRST and its errors come first, so an absent or
    # unparseable config is reported as the cause rather than being masked by
    # (or silently producing) an empty schema-error list.
    state_errors, warnings, detail = config_state_problems()
    errors = state_errors + list(cfg.validate())

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "source": str(cfg.source_path) if cfg.source_path else None,
                    "state": detail["state"],
                    "peers": detail["peers"],
                    "resolution": detail["resolution"],
                    "warnings": warnings,
                    "errors": errors,
                },
                indent=2,
            )
        )
    else:
        for w in warnings:
            console.print(f"[yellow]warning:[/yellow] {w}")
        for e in errors:
            console.print(f"[red]error:[/red] {e}")
        if not errors and not warnings:
            console.print(
                f"[green]ok[/green]  config.yaml is valid ({detail['peers']} peer(s))"
            )
    if errors:
        raise SystemExit(1)


def register_validate_command(group: click.Group) -> None:
    """Attach the ``validate`` command onto the ``host`` group."""
    group.add_command(host_validate)
