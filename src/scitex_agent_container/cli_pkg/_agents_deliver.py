"""``sac agents deliver`` — send a message and REPORT whether it actually landed.

The operator-facing half of :mod:`.._delivery`. ``sac agents send`` already exists
and is the right tool when the target has a recorded session id — but measured on
the live host, only a handful of agents do. The TUI population, which is most of
the fleet, has none, so for those agents ``send`` cannot deliver at all and the
only route is a tmux paste, which until now reported success unconditionally.

This verb is ONE verb with TWO strategies: it prefers the existing ``send`` path
when a session id exists and falls back to the verified tmux path otherwise. What
it adds in both cases is an ANSWER — a tri-state verdict with every signal named,
its reason recorded, and the raw pane captures attached, so a delivery can be
re-examined later instead of merely believed.

Exit code is ONLY the summary. Every raw signal is in ``--json``, and a caller
that needs to distinguish an UNKNOWN (2) from click's own usage error (also 2)
MUST read the JSON rather than branch on the integer.
"""

from __future__ import annotations

import json

import click

from .._delivery import (
    DEFAULT_ARRIVAL_TIMEOUT_S,
    DEFAULT_IDLE_WAIT_S,
    DEFAULT_MAX_RESENDS,
    STRATEGY_SDK,
    STRATEGY_TUI,
    assess_delivery,
    deliver,
)
from ._helpers import _json_flag, agent_name_complete, console

#: How a verdict renders. UNKNOWN is MAGENTA — grouped with nothing green, since
#: the failure being fixed is an unverified send that looked like a delivered one.
_STYLE = {
    True: ("green", "DELIVERED"),
    False: ("red", "NOT DELIVERED"),
    None: ("magenta", "UNKNOWN"),
}


@click.command(name="deliver")
@click.argument("name", shell_complete=agent_name_complete)
@click.argument("message")
@click.option(
    "--strategy",
    type=click.Choice(["auto", STRATEGY_SDK, STRATEGY_TUI]),
    default="auto",
    show_default=True,
    help="Force a delivery strategy. 'auto' prefers the recorded-session send "
    "path and falls back to the verified tmux path.",
)
@click.option(
    "--arrival-timeout",
    type=float,
    default=DEFAULT_ARRIVAL_TIMEOUT_S,
    show_default=True,
    help="Seconds to watch for the injected token to render after the paste.",
)
@click.option(
    "--idle-wait",
    type=float,
    default=DEFAULT_IDLE_WAIT_S,
    show_default=True,
    help="Seconds to wait for the peer to go idle before each submit attempt. "
    "Keep this generous — a UserPromptSubmit hook alone can take 30s, and a "
    "short budget manufactures false 'wedged' readings.",
)
@click.option(
    "--max-resends",
    type=int,
    default=DEFAULT_MAX_RESENDS,
    show_default=True,
    help="Bounded submit retries. Each re-checks idle from scratch.",
)
@click.option("--json", "as_json", is_flag=True, help="Every RAW signal, as JSON.")
@click.pass_context
def agents_deliver(
    ctx: click.Context,
    name: str,
    message: str,
    strategy: str,
    arrival_timeout: float,
    idle_wait: float,
    max_resends: int,
    as_json: bool,
) -> None:
    """Deliver MESSAGE to agent NAME and verify it arrived AND was submitted.

    Unlike a bare ``tmux send-keys``, this resolves and PROVES the target exists,
    observes the pane before sending, confirms arrival by a short injected token
    matched against a flattened pane (so a re-render cannot fake a negative), and
    then confirms SUBMISSION — retrying the Enter only when the pane is idle,
    because the Ink TUI silently eats an Enter fired while it is busy.

    \b
    Examples:
      $ sac agents deliver scitex-dev "please rebase onto develop"
      $ sac agents deliver dotfiles "ping" --json
      $ sac agents deliver some-agent "hi" --strategy tui --idle-wait 120

    \b
    Exit codes (the summary only — read --json for the signals):
      0  delivered AND submitted
      1  refuted with complete information
      2  could NOT determine (do not resend on this — the message may have landed)
      3  no route: the session does not exist on a tmux server that can see others
      4  arrived but STILL UNSENT in the composer — attach and press Enter
    """
    use_json = _json_flag(ctx, as_json)

    state = deliver(
        name,
        message,
        strategy=strategy,
        arrival_timeout_s=arrival_timeout,
        idle_wait_s=idle_wait,
        max_resends=max_resends,
    )
    verdict = assess_delivery(state)
    code = verdict.exit_code()

    if use_json:
        click.echo(
            json.dumps(
                {**state.to_dict(), "assessment": verdict.to_dict()},
                indent=2,
                ensure_ascii=False,
            )
        )
        raise SystemExit(code)

    colour, label = _STYLE[verdict.verdict]
    console.print(
        f"[{colour}]{label}[/{colour}] {state.agent} "
        f"[dim](strategy={state.strategy or 'none'}, token={state.token}, "
        f"{state.elapsed or 0.0:.1f}s)[/dim]",
        soft_wrap=True,
    )
    for signal, value in state.signals().items():
        shown = "None" if value is None else str(value)
        tone = "magenta" if value is None else "dim"
        console.print(
            f"    [{tone}]{signal:<24} {shown:<5}[/{tone}] "
            f"[dim]{state.reason_for(signal)}[/dim]",
            soft_wrap=True,
        )
    console.print(f"    [{colour}]=> {verdict.reason}[/{colour}]", soft_wrap=True)
    console.print(f"[dim]exit {code}[/dim]", soft_wrap=True)
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``deliver`` to the parent ``agents`` Click group."""
    agent_group.add_command(agents_deliver)


__all__ = ["agents_deliver", "register"]

# EOF
