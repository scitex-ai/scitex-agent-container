"""``sac doctor`` — drift + health diagnostics.

Two local checks and one fleet check:

* ``sac doctor`` — check THIS host's agent-spec source repo against its
  remote (the same comparison the launch-time guard runs) AND assert that
  no two live Telegram pollers share a bot token, then print both verdicts.
* ``sac doctor --pollers`` — the poller check alone.
* ``sac doctor --fleet`` — ssh every configured peer and render a
  per-host drift table (current / N-behind / M-ahead / diverged /
  unreachable / not-a-repo).

``--strict`` makes a drifted result — or an alarming poller verdict — a
non-zero exit (CI gate). Both surfaces are resilient: an unreachable peer
is reported, never crashed.

WHY THE POLLER CHECK LIVES HERE, and why it is on by default: it observes a
fault that has never had an instrument. An orphaned ``telegram-server.ts``
survives its agent's container restart, the next start adds a second poller
for the same bot token, and Telegram 409s them both — dropping the operator's
inbound messages with no error anywhere. See
:mod:`..runtimes._cct_poller_singleton`. Every previous sighting came from an
agent happening to read its own log mid-incident; a check nobody has to
remember to run is the difference between that and a measurement.
"""

from __future__ import annotations

import json

import click

from .._drift import DriftState, DriftStatus, check_spec_source_drift
from .._drift._fleet import HostDrift, check_fleet_drift
from ..runtimes._cct_poller_singleton import (
    POLLER_OK,
    POLLER_UNKNOWN,
    POLLER_VIOLATION,
    PollerSingletonVerdict,
    check_poller_singleton,
)
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

# Rich style per poller state. UNKNOWN is yellow, never green: an unread
# instrument must not look like an all-clear.
_POLLER_STYLE = {
    POLLER_OK: "green",
    POLLER_VIOLATION: "red",
    POLLER_UNKNOWN: "yellow",
}


def _local_agents_spec_dir() -> str:
    """Path used as the local drift probe target (the agents dir).

    ``check_spec_source_drift`` resolves it to the git toplevel, so
    pointing at the agents dir is enough — no need for a concrete
    spec.yaml. On fleet hosts this symlinks into the dotfiles checkout.
    """
    from pathlib import Path

    return str(Path("~/.scitex/agent-container/agents").expanduser())


def _render_local_human(status: DriftStatus) -> None:
    """Print the local spec-source drift verdict."""
    style = _STATE_STYLE.get(status.state, "white")
    console.print(
        f"[bold]local spec-source[/bold]  [{style}]{status.summary()}[/{style}]"
    )
    if status.repo:
        console.print(f"  repo  {status.repo}")


def _render_pollers_human(verdict: PollerSingletonVerdict) -> None:
    """Print the poller-singleton verdict, and its remedy when alarming."""
    style = _POLLER_STYLE.get(verdict.state, "white")
    console.print(
        f"[bold]telegram pollers[/bold]  [{style}]{verdict.summary()}[/{style}]"
    )
    for poller in verdict.pollers:
        owner = poller.agent or "(agent unknown)"
        fingerprint = poller.token_fp or "(token unreadable)"
        console.print(f"  pid {poller.pid:<8} {fingerprint}  {owner}")
    if verdict.is_alarming:
        console.print(f"  [{style}]{verdict.detail}[/{style}]")
        console.print(f"  [bold]hint[/bold]  {verdict.hint()}")


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


def _render_local(
    ctx: click.Context,
    as_json: bool,
    strict: bool,
    *,
    with_drift: bool = True,
    with_pollers: bool = True,
) -> int:
    """Run + render the requested local checks. Returns exit code.

    ``--strict`` fails on a drifted spec source OR an alarming poller verdict
    — which includes UNKNOWN, matching ``sac agents cct-audit``: an invariant
    sac could not assert is not an invariant that held.
    """
    payload: dict = {}
    failed = False

    status = check_spec_source_drift(_local_agents_spec_dir()) if with_drift else None
    verdict = check_poller_singleton() if with_pollers else None

    if status is not None:
        payload["local"] = status.to_dict()
        failed = failed or status.is_drifted
    if verdict is not None:
        payload["pollers"] = verdict.to_dict()
        failed = failed or verdict.is_alarming

    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
    else:
        if status is not None:
            _render_local_human(status)
        if verdict is not None:
            _render_pollers_human(verdict)

    return 1 if (strict and failed) else 0


@click.command("doctor", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--fleet",
    "fleet",
    is_flag=True,
    default=False,
    help="ssh every configured peer and report each host's spec-source drift.",
)
@click.option(
    "--pollers",
    "pollers",
    is_flag=True,
    default=False,
    help=(
        "Run ONLY the poller-singleton check: is more than one live Telegram "
        "poller holding the same bot token on this host?"
    ),
)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero when any checked source is drifted, or the poller "
        "verdict is violation/unknown (CI gate)."
    ),
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
    pollers: bool,
    strict: bool,
    timeout: int,
    as_json: bool,
) -> None:
    """Diagnose spec-source drift and duplicate Telegram pollers.

    \b
    Examples:
      $ sac doctor                 # spec source vs remote + poller singleton
      $ sac doctor --pollers       # only: one live poller per bot token?
      $ sac doctor --fleet         # per-host drift table for every peer
      $ sac doctor --fleet --json
      $ sac doctor --fleet --strict   # exit 1 if any host drifted

    \b
    The poller check is READ-ONLY and three-valued — ok / violation /
    unknown. It reports duplicates; it does not kill, lock or prevent them,
    and an unreadable /proc/<pid>/environ is reported as unknown rather than
    quietly counted as fine. No bot token VALUE is ever read out: only an
    opaque sha256:<12hex> fingerprint appears anywhere in the output.
    """
    if fleet:
        code = _render_fleet(ctx, as_json, strict, timeout)
    elif pollers:
        code = _render_local(ctx, as_json, strict, with_drift=False)
    else:
        code = _render_local(ctx, as_json, strict)
    if code:
        raise SystemExit(code)


__all__ = ["doctor"]
