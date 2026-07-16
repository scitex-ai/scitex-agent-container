"""``sac host sync`` — one-way code sync, centre → remote.

Split out of :mod:`host_group` (which is at the project's 512-line
ceiling) and registered onto it by :func:`register`, exactly as
:mod:`._host_crud` does.

Rendering rule for this whole file: **there is no quiet success path.**
The operator's requirement is literal — "I want one-way synchronisation,
and I do not want it to be silent." So a no-op still prints what it
verified (which sha, which module path, which symbol), a refusal prints
the offending commits or files by name, and an override prints what it
overrode. A report nobody reads is what let a five-release-stale Spartan
checkout go unnoticed until someone looked by hand.
"""

from __future__ import annotations

import json

import click

from .._hostsync import (
    DEFAULT_REPO,
    Outcome,
    SyncResult,
    check_peer,
    exit_code_for,
    route_reports_to_cards,
    sync_peer,
    syncable_peers,
)
from .._state.host_config import load as _load_cfg
from ._helpers import _json_flag, console

# Colour per outcome. Refusals and failures are loud on purpose.
_STYLE = {
    Outcome.CURRENT: ("green", "current"),
    Outcome.SYNCED: ("green", "synced"),
    Outcome.DRIFTED: ("yellow", "DRIFTED"),
    Outcome.REFUSED: ("red", "REFUSED"),
    Outcome.UNDETERMINED: ("magenta", "UNKNOWN"),
    Outcome.FAILED: ("red", "FAILED"),
}


def _evidence(text: str) -> None:
    """Print one evidence line WITHOUT rich's word-wrap.

    A wrapped absolute path is a path you cannot grep out of a cron log,
    and every line here exists to be read back later. ``soft_wrap`` keeps
    long checkout paths and symbol signatures on one line.
    """
    console.print(text, soft_wrap=True)


def _print_evidence(result: SyncResult) -> None:
    """Print what we actually observed — never a summary we invented."""
    before = result.before
    after = result.after
    if before.repo:
        _evidence(f"    checkout   {before.repo}")
    if before.module:
        _evidence(f"    module     {before.module}")
    if before.target:
        head = (after.head if after else before.head) or "?"
        _evidence(f"    ref        {before.target} @ {before.target_sha[:12] or '?'}")
        _evidence(f"    head       {head[:12] or '?'}")
    # The symbol probe — the only honest evidence that the code that is
    # LOADED is the code we synced. A version string would lie here.
    symbol = (after.symbol if after else before.symbol) or ""
    if symbol:
        _evidence(f"    symbol     claim_port{symbol}")
    for line in before.ahead_commits:
        _evidence(f"    [red]ahead[/red]      {line}")
    for line in before.dirty_files:
        _evidence(f"    [yellow]dirty[/yellow]      {line}")
    if result.ci:
        _evidence(f"    ci         {result.ci.state.value} — {result.ci.detail}")


def _print_result(result: SyncResult) -> None:
    colour, label = _STYLE[result.outcome]
    _evidence(
        f"[{colour}]{label:<9}[/{colour}] {result.peer}  "
        f"[dim]{result.before.summary()}[/dim]"
    )
    _print_evidence(result)
    if result.applied and result.applied.message:
        for line in result.applied.message.splitlines():
            _evidence(f"    [dim]git: {line}[/dim]")
    if result.decision and result.decision.reason and not result.ok:
        for line in result.decision.reason.splitlines():
            _evidence(f"  [red]{line}[/red]" if line.strip() else "")
    for note in result.notes:
        _evidence(f"  [yellow]note:[/yellow] {note}")
    console.print("")


@click.command("sync")
@click.argument("peer", required=False)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help="READ-ONLY: report drift and exit non-zero on it. Mutates nothing.",
)
@click.option(
    "--all",
    "all_peers",
    is_flag=True,
    default=False,
    help="Every peer in config.yaml (skips glob patterns and the centre itself).",
)
@click.option(
    "--ref",
    default="",
    help="Git ref to reconcile to. Default: the peer's own @{upstream}.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Override the CI-idle guard ONLY, printing what it overrides. It does "
        "NOT unlock ahead/diverged/dirty — sac never discards remote work."
    ),
)
@click.option("--repo", default=DEFAULT_REPO, help="owner/name for the CI guard.")
@click.option("--timeout", type=int, default=120, help="Per-ssh wall-clock cap (s).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.option(
    "--alarm",
    is_flag=True,
    default=False,
    help=(
        "READ-ONLY (requires --check): route each peer's verdict to an "
        "idempotent scitex-todo card — upsert on drift/unknown, resolve on "
        "clean — so the shout is SEEN on the board, not just in a log. "
        "Mutates no peer."
    ),
)
@click.pass_context
def host_sync(
    ctx: click.Context,
    peer: str | None,
    check_only: bool,
    all_peers: bool,
    ref: str,
    force: bool,
    repo: str,
    timeout: int,
    as_json: bool,
    alarm: bool,
) -> None:
    """Reconcile a peer's sac checkout to the centre's code. One-way.

    The centre is the brain: code flows centre -> remote and a remote
    never originates it. This verb makes that enforceable instead of
    hoped for.

    \b
    Detect (read-only, cron-friendly — exits non-zero on drift):
      $ sac host sync --check spartan
      $ sac host sync --check --all --json
    \b
    Remedy (fast-forward only):
      $ sac host sync spartan
      $ sac host sync --all

    Preconditions, each refusing LOUDLY with the next command to run:

    \b
      dirty tree   the peer has uncommitted edits  -> refuse, list them
      AHEAD        the peer holds commits the centre lacks -> refuse and
                   print them. That is a bug report, not a branch to
                   reconcile: sac will not merge them back (it would make
                   the remote a source of truth) nor discard them.
      CI busy      the peer's runners are working. On Spartan the sac
                   checkout IS the runner's audit workspace, so a merge
                   landing under a live job silently corrupts it.
      UNKNOWN      the peer could not be read. Unknown is not clean.

    Verification never trusts a version string — those are proven liars.
    After the fast-forward sac re-probes the peer and asserts that HEAD is
    the sha it aimed at, that the interpreter LOADS sac from inside that
    very checkout, and that a real symbol imports out of it.

    Credentials are deliberately NOT distributed by this verb; when that
    is decided they should ride this same one-way, precondition-guarded
    channel rather than growing a second path to the same hosts.
    """
    if bool(peer) == all_peers:
        raise click.UsageError(
            "give exactly one of PEER or --all  "
            "(e.g. `sac host sync --check spartan` or `sac host sync --check --all`)"
        )
    if alarm and not check_only:
        # Structural safety: the alarm rides ONLY the read-only detector. It
        # must never be reachable from a mutating sync run — a scheduled
        # alarm that could fast-forward a peer is Stage 1, not this.
        raise click.UsageError(
            "--alarm only rides the read-only --check form (never a mutating "
            "sync).  Use:  sac host sync --check --all --alarm"
        )

    cfg = _load_cfg()
    targets = syncable_peers(cfg) if all_peers else [peer or ""]
    if not targets:
        raise click.UsageError(
            f"no syncable peers in {cfg.source_path} — add one with `sac host add`"
        )

    results: list[SyncResult] = []
    for name in targets:
        if check_only:
            results.append(check_peer(name, cfg.peers, ref=ref, timeout=timeout))
        else:
            results.append(
                sync_peer(
                    name,
                    cfg.peers,
                    ref=ref,
                    force=force,
                    repo=repo,
                    timeout=timeout,
                )
            )

    code = exit_code_for([r.outcome for r in results])

    # Make the shout SEEN: route each verdict to an idempotent board card
    # (upsert on drift/unknown, resolve on clean). This runs in BOTH output
    # modes — the card is the delivery, independent of the console report.
    alarm_outcome = route_reports_to_cards(results) if alarm else None

    if _json_flag(ctx, as_json):
        payload: dict = {
            "mode": "check" if check_only else "sync",
            "exit_code": code,
            "peers": [r.to_dict() for r in results],
        }
        if alarm_outcome is not None:
            payload["alarm"] = {
                "drifted": list(alarm_outcome.drifted),
                "undetermined": list(alarm_outcome.undetermined),
                "cleared": list(alarm_outcome.cleared),
                "failed": list(alarm_outcome.failed),
            }
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)

    mode = "check (read-only)" if check_only else "sync"
    console.print(f"[bold]sac host {mode}[/bold]  centre -> {len(results)} peer(s)\n")
    for result in results:
        _print_result(result)

    # Never silent: say what the verdict MEANS, not just what it was.
    drifted = [r.peer for r in results if r.outcome is Outcome.DRIFTED]
    if check_only and drifted:
        console.print(
            f"[yellow]drift detected on {len(drifted)} peer(s):[/yellow] "
            f"{', '.join(drifted)}\n"
            "  These peers are NOT running the centre's code. Reconcile with:\n"
            f"    sac host sync {drifted[0]}"
        )
    elif code == 0:
        console.print(
            "[green]all peers match the centre[/green] "
            "[dim](verified by loaded-module path + symbol, not by version string)[/dim]"
        )
    if alarm_outcome is not None:
        console.print(f"[dim]{alarm_outcome.summary_line()}[/dim]")
    raise SystemExit(code)


def register(host_group) -> None:
    """Attach ``sync`` to the parent ``host`` Click group."""
    host_group.add_command(host_sync)


__all__ = ["host_sync", "register"]
