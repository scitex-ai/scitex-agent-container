"""``--no-dry-run``: open or resume the journal, drive the phases, print what happened.

The dry run answers "may this move". This answers "what did it actually do", and
the difference between the two is the entire risk surface, so the reporting here
is deliberately not a summary — every phase prints its own line, and every line
says whether that phase SUCCEEDED, FAILED or COULD NOT BE MEASURED.

RESUME IS THE DEFAULT, NOT A FLAG. A relocation touches two hosts and will
eventually be interrupted; :mod:`.._lifecycle._relocate_phases` was built around
that, and the journal it produces is now persisted
(:mod:`.._state.relocation_pg`, per-host PostgreSQL since 2026-08-28). So a
second invocation LOADS the stored journal and continues from where the first
stopped, rather than starting over —
and because :func:`advance` treats "advance to the phase you are already in" as a
no-op that succeeds, a coordinator that did the work and died before journalling
re-runs harmlessly.

A DIFFERENT DESTINATION IS A DIFFERENT RELOCATION. A stored journal moving the
agent to some other host is not resumed; that would silently retarget a move
someone else started. It is refused, and the refusal names the stored
destination, because the operator's next question is "to where, then".

THE PREFLIGHT STILL GATES. ``--no-dry-run`` runs exactly the same checks and
refuses on the same verdict. It is a common instinct to treat the checks as
advisory once the executing path exists; they are the opposite — the executing
path is what makes them load-bearing.

THE EXIT CODE IS THE OUTCOME, and the three ways this ends are three codes, so a
script cannot mistake one for another: completed, stopped-and-understood, and
stopped-because-something-could-not-be-measured.
"""

from __future__ import annotations

from .._logging import render_rich
import time

from .._lifecycle._relocate_effects import adapters_for, build_effects
from .._lifecycle._relocate_execute import (
    CODE_COMPLETED,
    ExecuteOutcome,
    execute,
)
from .._lifecycle._relocate_phases import begin
from .._state.relocation_pg import load_journal, save_journal
from ._helpers import console

__all__ = ["EXIT_INCOMPLETE", "EXIT_UNMEASURED", "run_relocation"]

#: A phase refused and the refusal is understood. The relocation stopped in a
#: named state that the journal records; a re-run resumes from it.
EXIT_INCOMPLETE = 5
#: A phase could not be MEASURED. Refuses as firmly, and calls for a different
#: action — go and measure it, rather than go and fix it — so it gets its own code.
EXIT_UNMEASURED = 6


def _local_host() -> str:
    """The fleet name of the host this coordinator's commands land on.

    Read through :func:`.._state.state_store_hostname.resolve_host`, the same
    resolver every state-db write uses ($SAC_HOST, then ``config.yaml``'s
    canonical, then the short hostname), rather than calling
    ``socket.gethostname`` here. Two answers to "which host am I" is how a
    relocation ends up ssh-ing to itself under one name and writing rows under
    another.

    It decides ONE thing — whether a command goes through ssh or runs directly —
    so a wrong "this is not me" costs an extra hop and nothing else.
    """
    from .._state.state_store_hostname import resolve_host

    return resolve_host(None)


def run_relocation(
    *, name: str, spec: dict, from_host: str, to_host: str, spec_path: str = ""
) -> ExecuteOutcome:
    """Drive the relocation and print each phase as it resolves."""
    stored = load_journal(name)
    if stored is not None and stored.to_host != to_host:
        render_rich(f"[red]refusing:[/red] a relocation of {name} to {stored.to_host!r} is already "
            f"in flight at phase {stored.phase!r}. Finish or abort that one before "
            f"starting a move to {to_host!r} — resuming it under a new destination would "
            "retarget a move somebody else started.", __name__)
        raise SystemExit(EXIT_INCOMPLETE)

    if stored is not None and not stored.is_terminal:
        relocation = stored
        render_rich(f"resuming the stored relocation of {name}: it reached [bold]{stored.phase}[/bold]", __name__)
    else:
        relocation = begin(
            agent=name,
            from_host=from_host,
            to_host=to_host,
            now=time.time(),
            detail=f"opened by sac agents relocate --no-dry-run on {_safe_local()}",
        )
        save_journal(relocation)

    adapters = adapters_for(
        agent=name,
        spec=spec,
        spec_path=spec_path,
        from_host=from_host,
        to_host=to_host,
        local_host=_safe_local(),
    )
    outcome = execute(relocation, effects=build_effects(adapters), now=time.time)
    save_journal(outcome.relocation)

    _render(outcome, adapters)
    return outcome


def _safe_local() -> str:
    try:
        return _local_host()
    except Exception as exc:  # stx-allow: fallback (reason: an unresolvable local name must not abort the run; it only decides ssh-vs-local, and a wrong "not local" merely costs one ssh hop)
        render_rich(f"[yellow]note:[/yellow] the local host name could not be resolved "
            f"({type(exc).__name__}); every command will go through ssh, including any "
            "aimed at this machine", __name__)
        return ""


def _render(outcome: ExecuteOutcome, adapters) -> None:
    render_rich("", __name__)
    render_rich("[bold]PHASES[/bold]", __name__)
    for line in outcome.log:
        render_rich(f"  {line}", __name__)

    if adapters.log:
        render_rich("", __name__)
        render_rich("[bold]EVIDENCE[/bold]", __name__)
        for line in adapters.log:
            render_rich(f"  {line}", __name__)

    if adapters.sent:
        render_rich("", __name__)
        render_rich("[bold]MEASURED[/bold]  (source -> target, counted on each host)", __name__)
        landed = {f.name: f for f in adapters.landed}
        for f in adapters.sent:
            there = landed.get(f.name)
            render_rich(f"  {f.name}\n"
                f"    {adapters.from_host}: {f.byte_count} bytes / {f.line_count} lines\n"
                f"    {adapters.to_host}: "
                + (
                    f"{there.byte_count} bytes / {there.line_count} lines"
                    if there is not None
                    else "ABSENT"
                ), __name__)

    render_rich("", __name__)
    if outcome.completed is True:
        render_rich(f"[green]DONE[/green]  {outcome.reason}", __name__)
        return

    label = "UNMEASURED" if outcome.completed is None else "STOPPED"
    render_rich(f"[yellow]{label}[/yellow] at [bold]{outcome.stopped_at}[/bold]: {outcome.reason}", __name__)
    render_rich(f"  next: {outcome.hint}", __name__)
    render_rich(f"  state: phase={outcome.relocation.phase} "
        f"source_left_stopped={outcome.source_left_stopped} "
        f"standby_left_running={outcome.standby_left_running} "
        f"past_no_return={outcome.past_no_return}", __name__)
    render_rich("  nothing was deleted. Anything moved went to a .old/<stamp>/ directory on the "
        "host it was moved on; a rollback is you moving it back, deliberately.", __name__)


def exit_code_for(outcome: ExecuteOutcome) -> int:
    """0 completed, 6 unmeasured, 5 stopped-and-understood. Never collapsed."""
    if outcome.completed is True and outcome.code == CODE_COMPLETED:
        return 0
    return EXIT_UNMEASURED if outcome.completed is None else EXIT_INCOMPLETE


__all__.append("exit_code_for")
