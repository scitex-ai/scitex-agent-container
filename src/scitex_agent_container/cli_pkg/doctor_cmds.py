"""``sac doctor`` — drift + health diagnostics.

Today the doctor surfaces spec-source git drift:

* ``sac doctor`` — check THIS host's agent-spec source repo against its
  remote (the same comparison the launch-time guard runs) and print the
  verdict.
* ``sac doctor --fleet`` — ssh every configured peer and render a
  per-host drift table (current / N-behind / M-ahead / diverged /
  unreachable / not-a-repo).

``--strict`` makes a drifted result a non-zero exit (CI gate). Both
surfaces are resilient: an unreachable peer is reported, never crashed.
"""

from __future__ import annotations

import json

import click

from .._drift import DriftState, check_spec_source_drift
from .._drift._fleet import HostDrift, check_fleet_drift
from ._helpers import _json_flag, console

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# Rich style per drift state for the human table.
_STATE_STYLE = {
    DriftState.CURRENT: "green",
    DriftState.BEHIND: "yellow",
    DriftState.AHEAD: "yellow",
    DriftState.DIVERGED: "red",
    DriftState.NOT_A_REPO: "dim",
    DriftState.UNREACHABLE: "dim",
}


def _local_agents_spec_dir() -> str:
    """Path used as the local drift probe target (the agents dir).

    ``check_spec_source_drift`` resolves it to the git toplevel, so
    pointing at the agents dir is enough — no need for a concrete
    spec.yaml. On fleet hosts this symlinks into the dotfiles checkout.
    """
    from pathlib import Path

    return str(Path("~/.scitex/agent-container/agents").expanduser())


def _render_local(ctx: click.Context, as_json: bool, strict: bool) -> int:
    """Run + render the local spec-source drift check. Returns exit code."""
    status = check_spec_source_drift(_local_agents_spec_dir())
    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"local": status.to_dict()}, indent=2))
    else:
        style = _STATE_STYLE.get(status.state, "white")
        console.print(
            f"[bold]local spec-source[/bold]  [{style}]{status.summary()}[/{style}]"
        )
        if status.repo:
            console.print(f"  repo  {status.repo}")
    if strict and status.is_drifted:
        return 1
    return 0


def _render_fleet(ctx: click.Context, as_json: bool, strict: bool, timeout: int) -> int:
    """ssh every peer, render the drift table. Returns exit code."""
    from .._state.host_config import load as _load_host_config

    cfg = _load_host_config()
    peers = cfg.peers
    rows: list[HostDrift] = check_fleet_drift(peers, timeout=timeout)
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "config_path": str(cfg.source_path) if cfg.source_path else None,
                    "peers": [r.to_dict() for r in rows],
                },
                indent=2,
            )
        )
    elif not rows:
        console.print(
            "[dim](no peers configured — nothing to drift-check. "
            "Add peers with `sac host add <name> --ssh ...`.)[/dim]"
        )
    else:
        console.print("[bold]fleet spec-source drift[/bold]")
        width = max((len(r.host) for r in rows), default=4)
        for r in rows:
            style = _STATE_STYLE.get(r.status.state, "white")
            line = f"  {r.host:<{width}}  [{style}]{r.status.summary()}[/{style}]"
            console.print(line)
    drifted = [r for r in rows if r.status.is_drifted]
    if strict and drifted:
        return 1
    return 0


@click.command("doctor", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--fleet",
    "fleet",
    is_flag=True,
    default=False,
    help="ssh every configured peer and report each host's spec-source drift.",
)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    default=False,
    help="Exit non-zero when any checked source is drifted (CI gate).",
)
@click.option(
    "--timeout",
    type=int,
    default=30,
    show_default=True,
    help="Per-peer ssh timeout (seconds) for the --fleet check.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def doctor(
    ctx: click.Context,
    fleet: bool,
    strict: bool,
    timeout: int,
    as_json: bool,
) -> None:
    """Diagnose agent-spec source drift (local, or --fleet across hosts).

    \b
    Examples:
      $ sac doctor                 # this host's spec source vs its remote
      $ sac doctor --fleet         # per-host drift table for every peer
      $ sac doctor --fleet --json
      $ sac doctor --fleet --strict   # exit 1 if any host drifted
    """
    if fleet:
        code = _render_fleet(ctx, as_json, strict, timeout)
    else:
        code = _render_local(ctx, as_json, strict)
    if code:
        raise SystemExit(code)


__all__ = ["doctor"]
