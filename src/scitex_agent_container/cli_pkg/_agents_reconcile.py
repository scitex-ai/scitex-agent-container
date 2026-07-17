"""``sac agents reconcile`` — restart agents that DIED, and only those.

Registered onto ``sac agents`` by :func:`register`, the same way
:mod:`._host_sync` attaches to ``sac host``. The engine lives in
:mod:`.._reconcile`; this module is the operator's window onto it.

Rendering rule, inherited from ``sac host sync``: **there is no quiet
path.** Every agent gets a line saying what we concluded and WHY —
including, above all, the ones we deliberately did NOT touch. A silent skip
is indistinguishable from a bug, and this command exists precisely because
a failure nobody could see went unnoticed for hours.
"""

from __future__ import annotations

import json

import click

from .._reconcile import DEFAULT_PASS_CAP, Verdict, reconcile_pass
from ._helpers import _json_flag, console

#: Colour per verdict. Anything that leaves an agent DOWN is loud on purpose.
_STYLE = {
    Verdict.OK: ("green", "OK"),
    Verdict.RESTARTED: ("green", "RESTARTED"),
    Verdict.SKIPPED: ("dim", "SKIPPED"),
    Verdict.NOT_MANAGED: ("dim", "NOT-MANAGED"),
    Verdict.WOULD_RESTART: ("yellow", "WOULD-RESTART"),
    Verdict.COOLING_DOWN: ("yellow", "COOLING-DOWN"),
    Verdict.CAPPED: ("yellow", "CAPPED"),
    Verdict.OVER_BUDGET: ("red", "OVER-BUDGET"),
    Verdict.FAILED: ("red", "FAILED"),
    Verdict.UNKNOWN: ("magenta", "UNKNOWN"),
    Verdict.BUDGET_UNKNOWN: ("magenta", "BUDGET-UNKNOWN"),
    Verdict.RESTART: ("yellow", "RESTART"),
}

#: Verdicts worth a per-agent line by default. The fleet is ~93 specs and
#: most are healthy, so OK/NOT-MANAGED collapse into the summary unless
#: ``--verbose`` asks for them. SKIPPED always prints: a skip is a DECISION
#: about a DEAD agent, and hiding it is how "why didn't it come back?"
#: becomes an hour of confusion.
_ALWAYS_SHOWN = (
    Verdict.SKIPPED,
    Verdict.WOULD_RESTART,
    Verdict.RESTARTED,
    Verdict.FAILED,
    Verdict.OVER_BUDGET,
    Verdict.COOLING_DOWN,
    Verdict.CAPPED,
    Verdict.UNKNOWN,
    Verdict.BUDGET_UNKNOWN,
)


def _print_report(report) -> None:
    colour, label = _STYLE[report.verdict]
    # soft_wrap: a wrapped agent name is one you cannot grep out of a cron log.
    console.print(f"[{colour}]{label:<14}[/{colour}] {report.name}", soft_wrap=True)
    console.print(f"    [dim]{report.detail}[/dim]", soft_wrap=True)


@click.command(name="reconcile")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY restart the corpses. Without this, nothing is restarted.",
)
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_PASS_CAP,
    show_default=True,
    help=(
        "Global cap on restarts in ONE pass — the blast radius of a single "
        "bad tick. The remainder is deferred to the next pass, not lost."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report only, mutate nothing. This is already the DEFAULT.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    help="Also print the healthy (OK) and unmanaged agents, not just the summary.",
)
@click.pass_context
def reconcile(
    ctx: click.Context,
    apply: bool,
    limit: int,
    dry_run: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """Restart agents that DIED unexpectedly. Dry-run by default.

    sac's specs say ``restart: {policy: on-failure}``, but nothing ever
    enforced it: the loop that reads that field runs on a daemon thread
    inside the short-lived ``sac agents start`` CLI and dies with it. This
    verb is the missing enforcer — the one thing that owns "should be
    running ⇒ is running".

    \b
    Detect (read-only — this is the DEFAULT, exits non-zero if anything is down):
      $ sac agents reconcile
      $ sac agents reconcile --json
    \b
    Remedy:
      $ sac agents reconcile --apply

    THE RULE — tmux is the fact, intent is sacred, and only corpses move:

    \b
      session EXISTS      alive → hands off. A live-but-wedged agent is
                          auth-heal's job; touching it would destroy context.
      could NOT look      UNKNOWN. A container's tmux is a different
                          namespace and reports an EMPTY fleet — so absence
                          of evidence never becomes evidence of death.
      stopped / deleted   the operator ended it. That decision is sacred.
      never started       no instances row: starting it would be a start
                          nobody asked for, not a restart.
      remote / other host its tmux is not ours to read.
      ghost active row    session gone but the row still claims ACTIVE, or
                          crashed / reboot-swept → it DIED. RESTART.

    Restarting a corpse is safe precisely because it is a corpse: no
    session, so no context to lose. Rate limits (a per-agent 30-minute
    debounce, at most 2/hour, and --limit per pass) mean a persistently
    dying agent gets a BOARD CARD rather than an infinite bounce — a restart
    loop is worse than a down agent.

    Exits 0 clean, 1 if something is down, 2 if it could not see the fleet.
    """
    if apply and dry_run:
        raise click.UsageError(
            "--apply and --dry-run are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to restart."
        )

    outcome = reconcile_pass(apply=apply, limit=limit)
    code = outcome.exit_code()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry-run",
                    "exit_code": code,
                    "counts": outcome.counts(),
                    "heartbeat_carded": outcome.heartbeat_ok,
                    "agents": [r.to_dict() for r in outcome.reports],
                },
                indent=2,
            )
        )
        raise SystemExit(code)

    mode = "apply" if apply else "dry-run (read-only)"
    console.print(
        f"[bold]sac agents reconcile[/bold]  {mode} — {len(outcome.reports)} spec(s)\n"
    )
    shown = _ALWAYS_SHOWN + ((Verdict.OK, Verdict.NOT_MANAGED) if verbose else ())
    for report in outcome.reports:
        if report.verdict in shown:
            _print_report(report)

    counts = outcome.counts()
    console.print(
        "\n[bold]"
        + ("  ".join(f"{k}={v}" for k, v in counts.items()) or "nothing")
        + "[/bold]"
    )

    # Never silent: say what the verdict MEANS, not just what it was.
    would = outcome.of(Verdict.WOULD_RESTART)
    if would:
        console.print(
            f"\n[yellow]{len(would)} agent(s) DIED and would be restarted:[/yellow] "
            f"{', '.join(r.name for r in would)}\n"
            "  Nothing was restarted — this is a dry-run. To act:\n"
            "    sac agents reconcile --apply"
        )
    down = outcome.of(Verdict.FAILED, Verdict.OVER_BUDGET)
    if down:
        console.print(
            f"\n[red]{len(down)} agent(s) are DOWN and sac could NOT recover "
            f"them:[/red] {', '.join(r.name for r in down)}\n"
            "  Each has a board card naming it. A human needs to look — "
            "restarting is not fixing these."
        )
    if outcome.of(Verdict.BUDGET_UNKNOWN):
        console.print(
            "\n[magenta]sac could not read its OWN restart history[/magenta] "
            "— so the debounce and the hourly cap cannot be enforced, and an "
            "unenforceable budget is not a budget. It has REFUSED to restart "
            "anything rather than risk a loop; dead agents are staying dead.\n"
            "  A board card names it. Check the state root (on some hosts "
            "~/.scitex is a symlink into a revocable project), or pin the "
            "state somewhere durable:\n"
            "    export SAC_RECONCILE_HISTORY=/var/tmp/sac-fleet-reconcile.json"
        )
    if outcome.of(Verdict.UNKNOWN):
        console.print(
            "\n[magenta]UNKNOWN is NOT clean[/magenta] — sac could not read "
            "the fleet's tmux (are we inside a container? is tmux wedged?), "
            "so nothing was inferred and nothing was restarted."
        )
    elif code == 0:
        console.print(
            "\n[green]every agent sac promised to keep running is running[/green] "
            "[dim](verified against the host's tmux, not the registry's "
            "session_id — that field is a hypothesis)[/dim]"
        )
    if not outcome.heartbeat_ok:
        console.print(
            "[yellow]note:[/yellow] the reconciler's heartbeat card was NOT "
            "written — the board cannot tell anyone whether this enforcer is "
            "alive. See the stderr line above."
        )
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``reconcile`` to the parent ``agents`` Click group."""
    agent_group.add_command(reconcile)


__all__ = ["reconcile", "register"]
