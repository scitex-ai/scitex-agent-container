"""``sac agents state`` — ONE state shape for every agent, tri-state throughout.

The operator's window onto :mod:`.._agentstate`. Every other agent-facing verb
returns its own ad-hoc shape (list columns, an auth-status table, a restart line),
which is why ``auth-status`` and ``list`` could be asked minutes apart on one host
and return DIFFERENT POPULATIONS — 12 agents versus 11 — with neither able to
notice the other's disagreement. This verb returns the same dataclass for every
agent, always, with every signal True / False / **None**.

Two things it does that no existing verb does:

* an agent it could not read at all still gets a ROW (all-``None``), so absence is
  reported instead of being invisible;
* every reading is ARCHIVED with its raw pane captures, so "what did this agent's
  pane look like at 20:20?" is answerable later by ``grep`` rather than by
  re-running a probe that can no longer see that moment.

Exit code is ONLY the summary — 0 / 1 / 2. Every raw signal is in ``--json``.
"""

from __future__ import annotations

import json

import click

from .._agentstate import (
    append_state,
    assess,
    journal_path,
    observe_fleet,
)
from .._agentstate._observe import DEFAULT_INTERVAL
from ._helpers import _json_flag, console

#: How a verdict renders. UNKNOWN is MAGENTA — grouped with nothing green, since
#: the entire failure being fixed is an unread agent that looked like a fine one.
_STYLE = {
    True: ("green", "OK"),
    False: ("red", "PROBLEM"),
    None: ("magenta", "UNKNOWN"),
}


def _roster() -> tuple[list[str], str]:
    """The REGISTERED agent names — the population, not an enumeration of it.

    Reuses ``auth-heal``'s roster (which itself reuses ``fleet-reconcile``'s
    registry enumeration) rather than inventing a third source of truth, so all
    three sweeps can never disagree about which agents exist.
    """
    from .._authheal._detect import registered_agents

    roster = registered_agents()
    return list(roster.names), roster.detail


def _fleet_exit_code(assessments) -> int:
    """2 if anything is UNKNOWN, else 1 if anything is refuted, else 0.

    UNKNOWN outranks a problem for the same reason it does per-agent: "we could
    not determine this" must never be reported as a clean fleet, and 0 is the
    strongest claim available here. A pass that read nothing at all must not spell
    the same code as a pass that read everything and found it healthy.

    AN EMPTY LIST IS 2, NOT 0. Assessing nobody is not a finding about everybody,
    and "the enumeration came back empty" is the single most common way "we
    observed nothing" gets recorded as "the fleet is fine" — it is how a blind
    tmux read inside a container reports a healthy host, and how an unscheduled
    remediator logged success on every tick. The clean code has to be EARNED by
    having actually read something.
    """
    if not assessments:
        return 2
    if any(a.verdict is None for a in assessments):
        return 2
    if any(a.verdict is False for a in assessments):
        return 1
    return 0


@click.command(name="state")
@click.argument("names", nargs=-1)
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_INTERVAL,
    show_default=True,
    help="Seconds between the two pane captures used for the frozen check.",
)
@click.option("--json", "as_json", is_flag=True, help="Every RAW signal, as JSON.")
@click.option(
    "--no-journal",
    is_flag=True,
    default=False,
    help="Do NOT archive this reading. The archive is the point; skip it only "
    "for a throwaway check.",
)
@click.pass_context
def agents_state(
    ctx: click.Context,
    names: tuple[str, ...],
    interval: float,
    as_json: bool,
    no_journal: bool,
) -> None:
    """Report every agent's state as True / False / could-not-determine.

    With no NAMES, reads the whole REGISTERED roster — including agents with no
    live session, which render UNKNOWN rather than vanishing from the output.

    Each agent's signals are held flat and named (is_tmux_live, is_process_alive,
    is_login_required, is_at_idle_prompt, ...), each of them True, False, or None
    meaning COULD NOT DETERMINE. One pure rule folds them: True when every
    load-bearing signal is healthy, False when one refutes with no signal unread,
    and UNKNOWN when something load-bearing could not be read — naming which.

    \b
    Examples:
      $ sac agents state
      $ sac agents state scitex-hub --json
      $ sac agents state --json | jq '.agents[] | select(.assessment.verdict == null)'

    Every reading is appended with its RAW pane captures to the journal, so a
    verdict can be re-examined long after the pane is gone.

    Exits 0 only when every agent read True, 1 when any agent is refuted with
    complete information, and 2 when anything could not be determined.
    """
    use_json = _json_flag(ctx, as_json)

    roster_detail = ""
    if names:
        targets = list(names)
    else:
        targets, roster_detail = _roster()
        if not targets:
            message = (
                "no registered agents to read. NOTE this is not a claim that the "
                "fleet is healthy — it is a claim that the roster is empty "
                f"({roster_detail})"
            )
            if use_json:
                click.echo(json.dumps({"agents": [], "exit_code": 2, "note": message}))
            else:
                console.print(f"[magenta]{message}[/magenta]")
            raise SystemExit(2)

    states = observe_fleet(targets, interval=interval)
    assessments = [assess(s) for s in states]
    code = _fleet_exit_code(assessments)

    writes = []
    if not no_journal:
        writes = [append_state(s) for s in states]

    if use_json:
        click.echo(
            json.dumps(
                {
                    "exit_code": code,
                    "roster": roster_detail,
                    "journal": str(journal_path()) if not no_journal else None,
                    "agents": [
                        {**s.to_dict(), "assessment": a.to_dict()}
                        for s, a in zip(states, assessments)
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        raise SystemExit(code)

    for state, verdict in zip(states, assessments):
        colour, label = _STYLE[verdict.verdict]
        console.print(f"[{colour}]{label:<8}[/{colour}] {state.agent}", soft_wrap=True)
        for name, value in state.signals().items():
            shown = "None" if value is None else str(value)
            tone = "magenta" if value is None else "dim"
            reason = state.reason_for(name)
            console.print(
                f"    [{tone}]{name:<22} {shown:<5}[/{tone}] [dim]{reason}[/dim]",
                soft_wrap=True,
            )
        console.print(f"    [{colour}]=> {verdict.reason}[/{colour}]\n", soft_wrap=True)

    failed_writes = [w for w in writes if not w.ok]
    if failed_writes:
        console.print(
            f"[red]{len(failed_writes)} reading(s) were NOT archived:[/red] "
            f"{failed_writes[0].detail}"
        )
    elif writes:
        cut = sum(len(w.truncated) for w in writes)
        note = f" ({cut} capture(s) truncated, each MARKED)" if cut else ""
        console.print(
            f"[dim]archived {len(writes)} reading(s) to {journal_path()}{note}[/dim]"
        )

    unknown = [a for a in assessments if a.verdict is None]
    if unknown:
        console.print(
            f"\n[magenta]{len(unknown)} agent(s) could NOT be determined:[/magenta] "
            f"{', '.join(a.agent for a in unknown)}\n"
            "  Nothing was learned about these — they are neither healthy nor "
            "broken, and this run therefore CANNOT report a clean fleet."
        )
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``state`` to the parent ``agents`` Click group."""
    agent_group.add_command(agents_state)


__all__ = ["agents_state", "register"]

# EOF
