"""``sac agents resume-rate-limited`` — wake agents whose rate wall has lifted.

The operator's window onto :mod:`.._ratelimit`, and the THIRD sibling of
``sac agents reconcile`` (corpses) and ``sac agents restart-login-expired``
(auth wedges). Those two divide agent liveness between them and a provider
rate wall falls in the gap: the tmux session is alive, so ``reconcile`` hands
off, and the banner is not an auth banner, so the auth healer's matcher
excludes it — deliberately, saying *a restart does not fix a rate wall*.

Which is true, and points at what this verb does instead. A rate wall is a
PAUSE WITH A PUBLISHED END TIME. Nothing is broken; the agent simply stopped
mid-turn and nothing inside it will ever notice the wall came down, because
the thing that would have noticed IS the turn that stopped. So this verb
waits for the reset the provider itself printed, and then continues the agent
— it never restarts one, because restarting would destroy the context that
made resuming worth doing.

Rendering rule (inherited from the siblings): there is no quiet path. Every
agent this pass touched gets a line saying what we concluded and WHY.
"""

from __future__ import annotations

import json

import click

from .._ratelimit import DEFAULT_INTERVAL, DEFAULT_PASS_CAP, Verdict, resume_pass
from ._helpers import _json_flag, console

#: Colour per verdict. Anything that leaves an agent parked is loud on purpose;
#: magenta is reserved for "we could not determine this", which must never read
#: as a clean line.
_STYLE = {
    Verdict.RESUMED: ("green", "RESUMED"),
    Verdict.SWITCHED: ("green", "SWITCHED"),
    Verdict.WAITING: ("cyan", "WAITING"),
    Verdict.ALREADY_ON_TARGET: ("cyan", "ALREADY-ON-TARGET"),
    Verdict.WOULD_RESUME: ("yellow", "WOULD-RESUME"),
    Verdict.WOULD_SWITCH: ("yellow", "WOULD-SWITCH"),
    Verdict.COOLING_DOWN: ("yellow", "COOLING-DOWN"),
    Verdict.CAPPED: ("yellow", "CAPPED"),
    Verdict.OVER_BUDGET: ("red", "OVER-BUDGET"),
    Verdict.FAILED: ("red", "FAILED"),
    Verdict.SWITCH_FAILED: ("red", "SWITCH-FAILED"),
    Verdict.RESET_UNKNOWN: ("magenta", "RESET-UNKNOWN"),
    Verdict.SWITCH_UNVERIFIED: ("magenta", "SWITCH-UNVERIFIED"),
    Verdict.UNREADABLE: ("magenta", "UNREADABLE"),
    Verdict.BUDGET_UNKNOWN: ("magenta", "BUDGET-UNKNOWN"),
}

#: Verdicts printed per agent. The three omitted ones — NOT-MANAGED,
#: NO-SESSION and NOT-LIMITED — are the overwhelming majority of a healthy
#: fleet and say nothing an operator needs per agent; they are carried by the
#: count line instead. That is not tidiness: an earlier sibling printed its
#: equivalent population every pass and produced 93,778 identical lines in a
#: 32 MB timer log, which buries the handful of lines that matter.
_ALWAYS_SHOWN = tuple(_STYLE)


def _print_report(report) -> None:
    colour, label = _STYLE.get(report.verdict, ("white", report.verdict.value))
    # soft_wrap: a wrapped agent name is one you cannot grep out of a timer log.
    console.print(f"[{colour}]{label:<15}[/{colour}] {report.name}", soft_wrap=True)
    console.print(f"    [dim]{report.detail}[/dim]", soft_wrap=True)


@click.command(name="resume-rate-limited")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY wake the agents whose wall has lifted. Without this, nothing "
    "is touched.",
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="Report only, mutate nothing (dry-run). This is already the DEFAULT.",
)
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_PASS_CAP,
    show_default=True,
    help="Global cap on resumes in ONE pass — the blast radius of a bad tick.",
)
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_INTERVAL,
    show_default=True,
    help="Seconds between the two pane captures used for the frozen check.",
)
@click.option(
    "--switch-model",
    "switch_model",
    is_flag=True,
    default=False,
    help="Also handle the MODEL-CAP shape: an agent frozen behind a Fable cap "
    "is moved onto opus[1m] and kicked. OFF by default — without it this "
    "command behaves exactly as it did before the branch existed. Needs "
    "--apply to type anything.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for timers).")
@click.pass_context
def resume_rate_limited(
    ctx: click.Context,
    apply: bool,
    check: bool,
    limit: int,
    interval: float,
    switch_model: bool,
    as_json: bool,
) -> None:
    """Resume LIVE agents parked behind a provider rate wall that has RESET.

    \b
    Detect (read-only — the DEFAULT):
      $ sac agents resume-rate-limited
      $ sac agents resume-rate-limited --check --json
    \b
    Remedy:
      $ sac agents resume-rate-limited --apply

    THE RULE — the wall's own clock decides, and only a frozen pane qualifies:

    \b
      no session          a CORPSE. `sac agents reconcile`'s, not this verb's.
      pane ADVANCING      the agent is working (or quoting the incident in
                          prose). Never touched.
      wall STILL UP       HELD, on purpose, and this exits 0. Waiting is the
                          normal state during a limit, not a fault — and it is
                          what makes hammering a live limit impossible here.
      reset UNREADABLE    HELD and REPORTED (exit 2). A guessed reset is how a
                          reviver starts burning the quota that ends the outage.
      wall LIFTED         resumed, through the VERIFIED delivery path, which
                          proves the nudge left the compose box. A nudge that
                          merely lands in the composer and is never submitted
                          is indistinguishable from the outage itself.

    \b
    THE MODEL-CAP BRANCH (--switch-model, OFF by default):
      $ sac agents resume-rate-limited --switch-model
      $ sac agents resume-rate-limited --apply --switch-model

    A rate wall publishes an end; a MODEL cap does not. Measured 2026-09-06:
    a Fable-family agent answered the operator's messages with "You've
    reached your Fable limit ... switch models with /model" — no reset
    clause, so the rule above correctly reports NOT-LIMITED and the agent
    stays silent forever. With --switch-model such an agent is moved onto
    opus[1m] instead of waited on: /model opus[1m], Enter to confirm, then a
    kick, THREE SECONDS APART, and finally a fresh capture that must PROVE
    the cap is gone. A switch that cannot be proven is SWITCH-UNVERIFIED and
    exits 2 — never a claimed recovery. Only agents on a Fable-family model
    are touched; every other agent keeps the verdict it has today.

    The agent is CONTINUED, never restarted: its session, context and
    conversation all survived the wall. Rate-limited exactly like its siblings
    (30-min/agent debounce, <=2/agent/hour, --limit per pass); an agent that
    cannot be woken is RECORDED as degraded rather than nudged forever.

    Exits 0 when nothing is parked or everything parked is legitimately
    waiting, 1 when a resume was owed and not delivered, and 2 when something
    could not be determined — an unreadable pane, an unreadable reset clause,
    or an unreadable resume history.
    """
    if apply and check:
        raise click.UsageError(
            "--apply and --check are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to resume."
        )

    outcome = resume_pass(
        apply=apply, limit=limit, interval=interval, switch_model=switch_model
    )
    code = outcome.exit_code()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "apply" if apply else "check",
                    "switch_model": switch_model,
                    "exit_code": code,
                    "counts": outcome.counts(),
                    "pass_recorded": outcome.heartbeat_ok,
                    "agents": [r.to_dict() for r in outcome.reports],
                },
                indent=2,
            )
        )
        raise SystemExit(code)

    mode = "apply" if apply else "check (read-only)"
    shown = outcome.of(*_ALWAYS_SHOWN)
    console.print(
        f"[bold]sac agents resume-rate-limited[/bold]  {mode} — "
        f"{len(shown)} agent(s) behind a rate wall, "
        f"{len(outcome.reports) - len(shown)} with nothing to report\n"
    )
    for report in shown:
        _print_report(report)

    counts = outcome.counts()
    if counts:
        console.print(
            "\n" + "  ".join(f"{k.lower()}={v}" for k, v in sorted(counts.items()))
        )

    stuck = outcome.of(Verdict.FAILED, Verdict.OVER_BUDGET, Verdict.SWITCH_FAILED)
    if stuck:
        console.print(
            f"\n[red]{len(stuck)} agent(s) are STILL parked and sac could not "
            f"get them working again.[/red] Each is recorded as degraded.",
            soft_wrap=True,
        )
    blind = outcome.of(
        Verdict.RESET_UNKNOWN, Verdict.UNREADABLE, Verdict.SWITCH_UNVERIFIED
    )
    if blind:
        console.print(
            f"\n[magenta]{len(blind)} agent(s) could not be DETERMINED.[/magenta] "
            f"A wall whose reset clause does not parse needs a new pattern in "
            f"_ratelimit._banner; a switch whose outcome the pane will not show "
            f"needs a human to look at that pane. Nothing here can resolve "
            f"either by guessing.",
            soft_wrap=True,
        )
    if not outcome.heartbeat_ok:
        console.print(
            "\n[yellow]note:[/yellow] this pass could not record that it RAN. "
            "A silent enforcer and a satisfied one look identical.",
            soft_wrap=True,
        )
    raise SystemExit(code)


def register(agent_group) -> None:
    agent_group.add_command(resume_rate_limited)
