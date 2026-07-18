"""``sac agents restart-login-expired`` — restart LIVE agents wedged on auth.

The operator's window onto :mod:`.._authheal`. Sibling of ``sac agents
reconcile``: reconcile restarts DEAD (no tmux session) agents; this restarts
LIVE ones whose Claude cannot authenticate — a frozen "Login expired" banner —
which reconcile explicitly leaves alone. Detection is READ-ONLY and
2-run-corroborated; the restart is rate-limited and escalates to a board card
rather than looping.

Rendering rule (inherited from ``sac agents reconcile``): there is no quiet
path. Every agent this pass touched gets a line saying what we concluded and
WHY — a silent skip is indistinguishable from a bug.

DEPLOY GATE: an existing ``auth-heal.py`` cron already restarts these agents.
Do NOT enable the ``sac.restart-login-expired-agents`` timer on a host until
that host's ``auth-heal.py`` ``scan_tui`` is retired — two restarters on one
fleet is the double-supervisor class. Running this command BY HAND is always
safe.
"""

from __future__ import annotations

import json

import click

from .._authheal import DEFAULT_INTERVAL, DEFAULT_PASS_CAP, auth_heal_pass
from .._reconcile._rule import Verdict
from ._helpers import _json_flag, console

#: Colour per verdict. Anything that leaves an agent wedged is loud on purpose.
_STYLE = {
    Verdict.RESTARTED: ("green", "RESTARTED"),
    Verdict.WOULD_RESTART: ("yellow", "WOULD-RESTART"),
    Verdict.COOLING_DOWN: ("yellow", "COOLING-DOWN"),
    Verdict.CAPPED: ("yellow", "CAPPED"),
    Verdict.OVER_BUDGET: ("red", "OVER-BUDGET"),
    Verdict.FAILED: ("red", "FAILED"),
    Verdict.BUDGET_UNKNOWN: ("magenta", "BUDGET-UNKNOWN"),
}


def _print_report(report) -> None:
    colour, label = _STYLE.get(report.verdict, ("white", report.verdict.value))
    # soft_wrap: a wrapped agent name is one you cannot grep out of a cron log.
    console.print(f"[{colour}]{label:<14}[/{colour}] {report.name}", soft_wrap=True)
    console.print(f"    [dim]{report.detail}[/dim]", soft_wrap=True)


@click.command(name="restart-login-expired")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY restart the wedged agents. Without this, nothing is restarted.",
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
    help="Global cap on restarts in ONE pass — the blast radius of a bad tick.",
)
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_INTERVAL,
    show_default=True,
    help="Seconds between the two pane captures used for the frozen check.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.pass_context
def restart_login_expired(
    ctx: click.Context,
    apply: bool,
    check: bool,
    limit: int,
    interval: float,
    as_json: bool,
) -> None:
    """Restart LIVE agents wedged behind a frozen "Login expired" banner.

    An agent whose API calls are rejected sits at its prompt under an auth
    banner forever — the tmux session is alive, so ``reconcile`` (which only
    ever touches a CORPSE) leaves it be, yet it does nothing. Claude never
    re-reads its credentials, so ONLY a restart clears it. This verb detects
    that state (a system auth banner frozen directly above the prompt across two
    captures — so a working agent QUOTING the banner is never mistaken for a
    wedged one) and restarts it, through the pool-loading start path so the
    restart cannot strip the agent's CCT/Telegram token.

    \b
    Detect (read-only — the DEFAULT; exits non-zero if anything is wedged):
      $ sac agents restart-login-expired
      $ sac agents restart-login-expired --check --json
    \b
    Remedy:
      $ sac agents restart-login-expired --apply

    Rate-limited exactly like ``reconcile`` (30-min/agent debounce, <=2/agent/
    hour, --limit per pass): an agent STILL wedged after the cap gets a BOARD
    CARD rather than an infinite bounce — a restart loop is worse than a wedged
    agent.

    \b
    ! DEPLOY GATE — the SCHEDULED form (sac.restart-login-expired-agents timer):
      An existing auth-heal.py cron ALREADY restarts these agents. Do NOT enable
      this timer on a host until that host's auth-heal.py scan_tui is retired —
      two restarters bouncing one fleet is the double-supervisor class. Running
      THIS COMMAND by hand is always safe.

    Exits 0 clean, 1 if something is wedged, 2 if it cannot read its own memory.
    """
    if apply and check:
        raise click.UsageError(
            "--apply and --check are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to restart."
        )

    outcome = auth_heal_pass(apply=apply, limit=limit, interval=interval)
    code = outcome.exit_code()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "apply" if apply else "check",
                    "exit_code": code,
                    "counts": outcome.counts(),
                    "heartbeat_carded": outcome.heartbeat_ok,
                    "agents": [r.to_dict() for r in outcome.reports],
                },
                indent=2,
            )
        )
        raise SystemExit(code)

    mode = "apply" if apply else "check (read-only)"
    console.print(
        f"[bold]sac agents restart-login-expired[/bold]  {mode} — "
        f"{len(outcome.reports)} corroborated login-expired agent(s)\n"
    )
    for report in outcome.reports:
        _print_report(report)

    counts = outcome.counts()
    console.print(
        "\n[bold]"
        + ("  ".join(f"{k}={v}" for k, v in counts.items()) or "nothing wedged")
        + "[/bold]"
    )

    would = outcome.of(Verdict.WOULD_RESTART)
    if would:
        console.print(
            f"\n[yellow]{len(would)} agent(s) are login-expired and would be "
            f"restarted:[/yellow] {', '.join(r.name for r in would)}\n"
            "  Nothing was restarted — this is a dry-run. To act:\n"
            "    sac agents restart-login-expired --apply"
        )
    down = outcome.of(Verdict.FAILED, Verdict.OVER_BUDGET)
    if down:
        console.print(
            f"\n[red]{len(down)} agent(s) are STILL wedged and sac could NOT heal "
            f"them:[/red] {', '.join(r.name for r in down)}\n"
            "  Each has a board card. A human needs to look — restarting is not "
            "fixing these (usually a real account problem)."
        )
    if outcome.of(Verdict.BUDGET_UNKNOWN):
        console.print(
            "\n[magenta]sac could not read its OWN restart history[/magenta] — so "
            "the debounce and the hourly cap cannot be enforced. It has REFUSED "
            "to restart anything rather than risk a loop.\n"
            "  Pin the state somewhere durable:\n"
            "    export SAC_LOGIN_EXPIRED_HISTORY=/var/tmp/sac-login-expired.json"
        )
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``restart-login-expired`` to the parent ``agents`` Click group."""
    agent_group.add_command(restart_login_expired)


__all__ = ["register", "restart_login_expired"]
