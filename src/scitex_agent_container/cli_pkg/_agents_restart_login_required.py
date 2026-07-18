"""``sac agents restart-login-required`` — find login-required agents, restart, log.

The operator's three lines, as one verb:

    1. login required となっているエージェントの自動特定
    2. sac agents restart -y <detected-agent>
    3. 全てログを取る

WHY IT IS NOT ``restart-login-expired``
    That sibling requires the banner to be FROZEN across two captures, so it
    calls an agent healthy whenever its pane still moves — and an agent can be
    thoroughly wedged while its spinner ticks, its clock counts or its pane
    reflows. Those are exactly the agents the operator has been restarting by
    hand. This verb keeps the anti-prose defence but takes it from the
    NEAR-PROMPT geometry of a SINGLE capture: a banner pinned in the
    conversation tail directly above the input prompt is the CURRENT UI STATE,
    while a banner an agent merely quoted sits up in scrollback because the
    agent kept producing output after it. Animation cannot hide a wedge from
    that question.

    Both verbs are kept. The freeze test is the more conservative of the two,
    and nothing is served by deleting a running detector to install its
    successor on the same night.

EVERYTHING IS LOGGED
    Every agent examined, its verdict and WHY, its raw pane, the exact argv
    executed, the exit code, and the full stdout and stderr — to a file beside
    the existing runtime logs. If that log cannot be written, this verb REFUSES
    to restart anything: an unauditable restart is precisely the failure it
    exists to end.
"""

from __future__ import annotations

import json

import click

from .._authheal import DEFAULT_PASS_CAP
from .._authheal._journal import log_path
from .._authheal._restart_pass import restart_login_required_pass
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
    # Magenta groups with BUDGET-UNKNOWN rather than with the yellow "wedged"
    # verdicts, because it says the same thing they do: we could not determine
    # this — the one colour that must never read as a clean line.
    Verdict.UNOBSERVED: ("magenta", "UNOBSERVED"),
}


def _print_report(report) -> None:
    colour, label = _STYLE.get(report.verdict, ("white", report.verdict.value))
    # soft_wrap: a wrapped agent name is one you cannot grep out of a cron log.
    console.print(f"[{colour}]{label:<14}[/{colour}] {report.name}", soft_wrap=True)
    console.print(f"    [dim]{report.detail}[/dim]", soft_wrap=True)


@click.command(name="restart-login-required")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY restart the detected agents. Without this, nothing is restarted.",
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
    "--log-file",
    "log_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Where to write the full log. Default: the runtime dir (see --help).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.pass_context
def restart_login_required(
    ctx: click.Context,
    apply: bool,
    check: bool,
    limit: int,
    log_file: str | None,
    as_json: bool,
) -> None:
    """Find agents showing login-required and restart them, logging every step.

    An agent whose API calls are rejected sits under an auth banner forever. Its
    tmux session is alive, so ``reconcile`` (which only ever touches a CORPSE)
    leaves it be, yet it does nothing, and Claude never re-reads its credentials
    — only a restart clears it.

    \b
    DETECTION — near-prompt, from ONE capture:
      An agent is LOGIN-REQUIRED when a system auth banner sits in the
      conversation tail directly above its input prompt, i.e. it is the
      CURRENT UI STATE. An agent that merely QUOTES the banner while working
      has it up in scrollback (its own later output pushed it there) and is
      NOT flagged. Unlike `restart-login-required`'s older sibling
      `restart-login-expired`, the pane does NOT have to be frozen — so an
      agent that is wedged while its spinner still ticks is caught.

    \b
    REMEDY — the invocation you already use by hand:
      sac agents restart -y <agent>
      run as a subprocess, with its exit code, stdout and stderr logged whole.

    \b
    Detect (read-only — the DEFAULT; exits non-zero if anything is wedged):
      $ sac agents restart-login-required
      $ sac agents restart-login-required --check --json
    \b
    Remedy:
      $ sac agents restart-login-required --apply

    Rate-limited exactly like `reconcile` (30-min/agent debounce, <=2/agent/
    hour, --limit per pass), on its OWN history file: an agent STILL wedged
    after the cap gets a BOARD CARD rather than an infinite bounce.

    \b
    THE LOG is the point. Every agent examined, its verdict, WHY it got that
    verdict (near-prompt hit / scrollback-only / pane unreadable), its raw pane
    capture, the exact argv, the exit code and the FULL stdout+stderr go to:
      $SAC_LOGIN_REQUIRED_LOG, else <runtime>/login-required-restart.log
    If that file cannot be written, this command REFUSES to restart anything
    rather than act with no record.

    Exits 0 ONLY when every registered agent was actually observed and none is
    wedged, 1 if something is wedged, and 2 if anything could not be determined
    — an unreadable pane, a registered agent with no live session, an
    unreadable fleet roster, or an unreadable restart history.
    """
    if apply and check:
        raise click.UsageError(
            "--apply and --check are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to restart."
        )

    from pathlib import Path

    target_log = Path(log_file) if log_file else log_path()
    outcome = restart_login_required_pass(apply=apply, limit=limit, log_file=target_log)
    code = outcome.exit_code()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "apply" if apply else "check",
                    "discriminator": "near-prompt",
                    "log_file": str(target_log),
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
    # Count the two populations SEPARATELY. A single total would fold the agents
    # we could not read into the agents we found wedged, which is the collapse
    # this command's exit code exists to keep apart.
    unseen = outcome.of(Verdict.UNOBSERVED)
    console.print(
        f"[bold]sac agents restart-login-required[/bold]  {mode} — "
        f"{len(outcome.reports) - len(unseen)} login-required agent(s), "
        f"{len(unseen)} NOT observed"
    )
    console.print(f"[dim]full log: {target_log}[/dim]\n")
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
            f"\n[yellow]{len(would)} agent(s) are login-required and would be "
            f"restarted:[/yellow] {', '.join(r.name for r in would)}\n"
            "  Nothing was restarted — this is a dry-run. To act:\n"
            "    sac agents restart-login-required --apply"
        )
    down = outcome.of(Verdict.FAILED, Verdict.OVER_BUDGET)
    if down:
        console.print(
            f"\n[red]{len(down)} agent(s) are STILL wedged and sac could NOT heal "
            f"them:[/red] {', '.join(r.name for r in down)}\n"
            f"  Each has a board card, and the restart's full output is in\n"
            f"    {target_log}\n"
            "  A human needs to look — restarting is not fixing these."
        )
    if unseen:
        console.print(
            f"\n[magenta]{len(unseen)} agent(s) were NOT observed:[/magenta] "
            f"{', '.join(r.name for r in unseen)}\n"
            "  Nothing was learned about these — they are neither healthy nor "
            "wedged, and this pass therefore CANNOT report a clean fleet.\n"
            "  Look at them by hand:\n"
            "    sac agents auth-status\n"
            "    sac agents list"
        )
    if outcome.of(Verdict.BUDGET_UNKNOWN):
        console.print(
            "\n[magenta]sac could not read its OWN restart history, or could not "
            "write its log[/magenta] — so either the rate limits cannot be "
            "enforced or the restarts could not be recorded. It has REFUSED to "
            "restart anything rather than loop, or act unauditably.\n"
            "  Pin both somewhere durable:\n"
            "    export SAC_LOGIN_REQUIRED_HISTORY=/var/tmp/sac-login-required.json\n"
            "    export SAC_LOGIN_REQUIRED_LOG=/var/tmp/sac-login-required.log"
        )
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``restart-login-required`` to the parent ``agents`` Click group."""
    agent_group.add_command(restart_login_required)


__all__ = ["register", "restart_login_required"]
