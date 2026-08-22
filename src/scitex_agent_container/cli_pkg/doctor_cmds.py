"""``sac doctor`` — drift + health diagnostics.

Three local checks and one fleet check:

* ``sac doctor`` — check THIS host's agent-spec source repo against its
  remote (the same comparison the launch-time guard runs), assert that no two
  live Telegram pollers share a bot token, and assert that no two SPECS
  resolve to the same one, then print all three verdicts.
* ``sac doctor --pollers`` — the live poller check alone.
* ``sac doctor --collisions`` — the static spec-collision check alone.
* ``sac doctor --fleet`` — ssh every configured peer and render a
  per-host drift table (current / N-behind / M-ahead / diverged /
  unreachable / not-a-repo).

``--strict`` makes a drifted result — or an alarming poller or collision
verdict — a non-zero exit (CI gate). Every surface is resilient: an
unreachable peer is reported, never crashed.

WHY THE POLLER CHECK LIVES HERE, and why it is on by default: it observes a
fault that has never had an instrument. An orphaned ``telegram-server.ts``
survives its agent's container restart, the next start adds a second poller
for the same bot token, and Telegram 409s them both — dropping the operator's
inbound messages with no error anywhere. See
:mod:`..runtimes._cct_poller_singleton`. Every previous sighting came from an
agent happening to read its own log mid-incident; a check nobody has to
remember to run is the difference between that and a measurement.

AND WHY THE COLLISION CHECK IS BESIDE IT RATHER THAN INSIDE IT: they are the
two halves of one invariant and neither can do the other's job. The poller
check reads ``/proc`` and is HOST-SCOPED — measured 2026-08-22, one bot token
was held on compute-04 and compute-03 at once and the per-host probe returned
ok on BOTH. The collision check reads SPECS and is fleet-wide and STATIC, so
it sees that pair before either process starts, and sees no process at all.
Killing the duplicate poller ended that incident and fixed nothing: the specs
still collided, so it would have returned on the next start. Two verdicts,
side by side, each naming the other's blind spot.
See :mod:`..runtimes._cct_token_collision`.
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
    SCOPE_NOTE,
    PollerSingletonVerdict,
    check_poller_singleton,
)
from ..runtimes._cct_token_collision import (
    COLLISION_OK,
    COLLISION_UNKNOWN,
    COLLISION_VIOLATION,
    TokenCollisionVerdict,
    check_token_collisions,
)
from ..runtimes._cct_token_collision import SCOPE_NOTE as COLLISION_SCOPE_NOTE
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

# Same three-valued palette for the static spec-collision check, and UNKNOWN
# is yellow here for the same reason: a census sac could not compute must not
# look like one that came back clean.
_COLLISION_STYLE = {
    COLLISION_OK: "green",
    COLLISION_VIOLATION: "red",
    COLLISION_UNKNOWN: "yellow",
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
        if poller.token_fp:
            mark = poller.token_fp
        elif poller.disabled:
            mark = "(no token — by design)"
        else:
            mark = "(token unreadable)"
        console.print(f"  pid {poller.pid:<8} {mark:<24}  {owner}")
    console.print(f"  [dim]{verdict.population()}[/dim]")
    console.print(f"  [dim]{SCOPE_NOTE}[/dim]")
    if verdict.is_alarming:
        console.print(f"  [{style}]{verdict.detail}[/{style}]")
        console.print(f"  [bold]hint[/bold]  {verdict.hint()}")


def _render_collisions_human(verdict: TokenCollisionVerdict) -> None:
    """Print the static spec-collision verdict, and its remedy when alarming."""
    style = _COLLISION_STYLE.get(verdict.state, "white")
    console.print(
        f"[bold]spec bot tokens[/bold]  [{style}]{verdict.summary()}[/{style}]"
    )
    for collision in verdict.collisions:
        mark = "cross-host" if collision.cross_host else "same host"
        console.print(f"  {collision.token_fp:<24} {collision.describe()}  ({mark})")
    console.print(f"  [dim]{verdict.population()}[/dim]")
    console.print(f"  [dim]{COLLISION_SCOPE_NOTE}[/dim]")
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
    with_collisions: bool = True,
) -> int:
    """Run + render the requested local checks. Returns exit code.

    ``--strict`` fails on a drifted spec source OR an alarming poller verdict
    OR an alarming collision verdict — each of which includes UNKNOWN,
    matching ``sac agents cct-audit``: an invariant sac could not assert is
    not an invariant that held.
    """
    payload: dict = {}
    failed = False

    status = check_spec_source_drift(_local_agents_spec_dir()) if with_drift else None
    verdict = check_poller_singleton() if with_pollers else None
    collisions = check_token_collisions() if with_collisions else None

    if status is not None:
        payload["local"] = status.to_dict()
        failed = failed or status.is_drifted
    if verdict is not None:
        payload["pollers"] = verdict.to_dict()
        failed = failed or verdict.is_alarming
    if collisions is not None:
        payload["token_collisions"] = collisions.to_dict()
        failed = failed or collisions.is_alarming

    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
    else:
        if status is not None:
            _render_local_human(status)
        if verdict is not None:
            _render_pollers_human(verdict)
        if collisions is not None:
            _render_collisions_human(collisions)

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
    "--collisions",
    "collisions",
    is_flag=True,
    default=False,
    help=(
        "Run ONLY the static spec-collision check: do two registered SPECS "
        "resolve to the same bot token? Fleet-wide; reads specs, not /proc."
    ),
)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero when any checked source is drifted, or the poller or "
        "collision verdict is violation/unknown (CI gate)."
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
    collisions: bool,
    strict: bool,
    timeout: int,
    as_json: bool,
) -> None:
    """Diagnose spec-source drift and duplicate Telegram bot tokens.

    \b
    Examples:
      $ sac doctor                 # drift + live pollers + spec collisions
      $ sac doctor --pollers       # only: one live poller per bot token?
      $ sac doctor --collisions    # only: do two SPECS take the same bot?
      $ sac doctor --fleet         # per-host drift table for every peer
      $ sac doctor --fleet --json
      $ sac doctor --fleet --strict   # exit 1 if any host drifted

    \b
    TWO HALVES OF ONE INVARIANT — Telegram admits ONE getUpdates consumer per
    bot token, globally:
      --pollers     reads /proc. Sees a LIVE duplicate, including an orphan
                    whose spec no longer asks for the rail. HOST-SCOPED.
      --collisions  reads SPECS + the secrets pool. Sees a duplicate BEFORE
                    anything starts, and across hosts. Sees no process.
    Measured 2026-08-22: one token was held on two hosts and the per-host
    poller probe returned ok on both. Neither check alone is an all-clear.

    \b
    Both are READ-ONLY and three-valued — ok / violation / unknown. They
    report duplicates; they do not kill, lock or refuse them, and an
    unreadable /proc/<pid>/environ or an inconclusive pool read is reported
    as unknown rather than quietly counted as fine. No bot token VALUE is
    ever read out: only an opaque sha256:<12hex> fingerprint appears anywhere
    in the output.
    """
    if fleet:
        code = _render_fleet(ctx, as_json, strict, timeout)
    elif pollers:
        code = _render_local(
            ctx, as_json, strict, with_drift=False, with_collisions=False
        )
    elif collisions:
        code = _render_local(ctx, as_json, strict, with_drift=False, with_pollers=False)
    else:
        code = _render_local(ctx, as_json, strict)
    if code:
        raise SystemExit(code)


__all__ = ["doctor"]
