"""``sac agents relocate <name> --to <host>`` — the dry run, and nothing else yet.

Relocation moves an agent to a different host. The AGENT relocates; the host
does not move, and the agent's identity and count are unchanged (1 -> 1). It is
not a kind of fork/twin, which changes WHAT an agent does and takes the count to
two — the two verbs share one implementation detail (seeding a session from an
existing transcript) and nothing else.

WHY THE DRY RUN SHIPS FIRST, ALONE. On 2026-08-07 this move was done by hand.
Rewriting `host:` alone produced an agent that STARTED, reported HEALTHY, and
did nothing — the worst failure shape there is, because it looks like success
and nobody goes looking. Every check in :mod:`.._lifecycle._relocate_preflight`
exists because of something that went wrong that day. Shipping the check before
the move means the next hand-move is already safer, even with no executing path.

THE EXECUTING PATH REFUSES rather than half-working. `--dry-run` is currently
required, and omitting it exits non-zero naming what is missing (the cross-host
transcript transport). A verb that silently does part of a relocation is exactly
the "looks like success" failure this command was written to prevent.

WHAT COMES FROM WHERE — the operator's requirement, 2026-08-08:
「定義されているのと、今動いているのって違うんで」

    DECLARED   read from the spec. Claims, printed as claims.
    OBSERVED   probed on the target. Evidence, printed separately.

Collapsing those into one column is how `sac agents list` reports a running
agent as `defined`, and this command must not repeat it.

UNPROBED IS UNKNOWN, AND UNKNOWN REFUSES. Any fact a probe could not answer
stays ``None``, which preflight reports as UNKNOWN and refuses on, exactly as it
would for a probe that ran and failed. That is deliberate: from the decision's
point of view "nobody asked" and "asked and could not tell" are the same thing,
and both differ from an answer. A dry run that cannot measure something is not a
green light — it is a list of what still has to be measured, each entry carrying
the reason it could not be.

THE PROBES ARE ONE ssh ROUND TRIP, NOT ELEVEN. See
:mod:`.._lifecycle._relocate_probe_adapter`: the batch is deliberately built so
that a partial answer degrades PER FACT — eight measured and three unknown,
never eleven of either because one section failed.
"""

from __future__ import annotations

import click

from .._lifecycle._relocate_render import render_dry_run
from ._helpers import console
from ._relocate_preflight_one import (
    Prepared,
    declared_from_spec,
    prepare_one,
    required_ports,
)

__all__ = ["declared_from_spec", "prepare_one", "register", "relocate"]

#: Exit code when the dry run refuses (a failed or undetermined check). Distinct
#: from click's own 2 (usage error) so a caller can tell "you asked wrongly"
#: from "the target is not ready".
EXIT_REFUSED = 3
#: RETIRED. It meant "the executing path does not exist"; it does now, and the
#: outcomes it used to cover are :data:`._relocate_run.EXIT_INCOMPLETE` (a phase
#: refused, understood) and :data:`._relocate_run.EXIT_UNMEASURED` (a phase could
#: not be measured). The number is not reused: a script written against the old
#: meaning must not silently start reading a new one.
EXIT_RETIRED_UNIMPLEMENTED = 4


#: What `--no-dry-run` can and cannot do, phase by phase. THE NOTICE IS
#: GENERATED FROM THIS LIST, so the two cannot drift apart the way the original
#: hard-coded sentence did — it named the transcript transport as the one missing
#: piece and went stale the moment that phase was built.
#:
#: Each entry is (phase, what is BUILT, what is MISSING). A phase with nothing
#: missing carries ``"—"`` there and RUNS. The split is the honest unit of
#: progress: a decision layer being unit-tested says nothing about whether bytes
#: move, and an adapter is not claimed to work until it has been exercised
#: against two real hosts.
_PHASE_READINESS: tuple[tuple[str, str, str], ...] = (
    (
        "source_drain",
        "_relocate_liveness (tmux is the same fact the runtime checks) — a STOPPED "
        "source drains vacuously and that is measured, not assumed",
        "no adapter for a RUNNING source: telling an agent to finish its in-flight "
        "work and take no new work, and confirming it did",
    ),
    (
        "source_stop",
        "_relocate_effects.stop_source: `sac agents stop` on the source, then a "
        "SECOND independent liveness observation. Idempotent",
        "—",
    ),
    (
        "transport",
        "_relocate_transport (selection, move-aside, byte+line verification), "
        "_relocate_transport_paths (target-side project dir), "
        "_relocate_transcript_home (the HOST path backing the container's $HOME) "
        "and _relocate_transport_ssh (tar-over-ssh; counts taken ON the target)",
        "—",
    ),
    (
        "target_standby",
        "_relocate_effects_standby: the spec is carried and byte+line verified on the "
        "target, the session_id marker is seeded from the CARRIED transcript "
        "(first boot only — an existing marker refuses rather than being overwritten) "
        "and confirmed by read-back, then `sac agents start --resume <carried uuid>` "
        "WITHOUT the lease and a SECOND independent liveness observation on BOTH hosts",
        "—",
    ),
    (
        "handshake",
        "_relocate_effects_handshake: the brief is delivered to the agent's sidecar on "
        "its own host and the answer is read back out of its transcript by the "
        "coordinator ON THE SOURCE, then put through _relocate_handshake's gate "
        "(nonce + a proof-of-work answer measured independently on the target)",
        "—",
    ),
    (
        "handover",
        "_relocate_effects_handover: the source's lease is bootstrapped when the store "
        "holds none (sac still does not claim one at agent start-up — the bootstrap is "
        "recorded as such), handed to the target, and the row is RE-READ to confirm "
        "the holder and the fence",
        "—",
    ),
    (
        "done",
        "_relocate_effects.finish: both hosts are observed for exactly ONE live "
        "instance, then residency is written to _state.state_db_relocation and the "
        "source's transcript MOVED ASIDE — all gated on the two confirmations being "
        "recorded True",
        "—",
    ),
)


def _readiness_notice() -> list[str]:
    """Say exactly which phases can and cannot run, before running any of them.

    Printed as a PREAMBLE now rather than as a refusal: the executing path
    exists, so the operator's question has changed from "why won't it run" to
    "how far will it get". Answering that up front is the difference between a
    surprising stop and an expected one.
    """
    lines = ["", "[bold]PHASE READINESS[/bold]"]
    for phase, built, missing in _PHASE_READINESS:
        state = "[green]runs[/green]" if missing == "—" else "[yellow]refuses[/yellow]"
        lines.append(f"  [bold]{phase}[/bold]  {state}")
        if built != "—":
            lines.append(f"    built:   {built}")
        if missing != "—":
            lines.append(f"    missing: {missing}")
    lines += [
        "",
        "A phase with no adapter returns UNKNOWN and the relocation STOPS there, in "
        "a state the journal records; it does not journal its way to DONE having "
        "moved nothing. Nothing is deleted at any point — anything displaced goes "
        "to .old/<stamp>/ on the host it was displaced on, so a rollback is you "
        "moving it back, deliberately.",
    ]
    return lines


#: Re-exported so callers (and the tests written against them) keep one import
#: site while the reading itself lives with the rest of the per-agent preflight.
_required_ports = required_ports


def _print_one(prepared: Prepared, dry_run: bool) -> None:
    """Print one agent's whole answer — notices, the report, or why there is none."""
    for notice in prepared.notices:
        console.print(f"[yellow]note:[/yellow] {notice}", soft_wrap=True)
    if prepared.error:
        colour = "yellow" if prepared.already_there else "red"
        console.print(f"[{colour}]{prepared.name}:[/{colour}] {prepared.error}")
        return
    assert prepared.report is not None
    for line in render_dry_run(
        prepared.report,
        declared=prepared.declared,
        errors=prepared.errors,
        dry_run=dry_run,
        workdir=prepared.workdir,
        from_host=prepared.from_host,
    ):
        console.print(line, soft_wrap=True)


def _sweep_summary(results: list[Prepared], to_host: str) -> list[str]:
    """One line per agent, so the SHAPE of the work is visible before any of it.

    The operator's reason for wanting several agents in one command: nine are
    queued, and learning "three need a dataset moved, four need an image, two are
    ready" is a different conversation from discovering it one relocation at a
    time, each costing a trip to another machine.
    """
    lines = ["", f"SWEEP — {len(results)} agent(s) against {to_host}", ""]
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        if r.already_there:
            verdict, note = "SKIP", "already recorded there"
        elif r.error:
            verdict, note = "ERROR", r.error.split(".")[0]
        elif r.report is None:
            verdict, note = "ERROR", "no report was produced"
        elif r.report.ok is True:
            verdict, note = "GO", "every check passed"
        else:
            n_f, n_u = len(r.report.failed), len(r.report.unknown)
            verdict = "REFUSED"
            note = f"{n_f} failed, {n_u} undetermined"
        lines.append(f"  {verdict:<8} {r.name:<{width}}  {note}")
    blocked = [r for r in results if r.blocks]
    lines.append("")
    lines.append(
        f"{len(results) - len(blocked)} of {len(results)} would proceed; "
        f"{len(blocked)} are blocked. Nothing was touched."
    )
    return lines


@click.command("relocate")
@click.argument("names", nargs=-1, required=True)
@click.option("--to", "to_host", required=True, help="Target host to relocate ONTO.")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Check the target and touch nothing. Several NAMES require --dry-run.",
)
def relocate(names: tuple[str, ...], to_host: str, dry_run: bool) -> None:
    """Relocate agent NAMES onto --to HOST. The AGENT moves, not the host.

    \b
    Examples:
      $ sac agents relocate scitex-dev --to scitex-compute-03 --dry-run
      $ sac agents relocate scitex-dev scitex-hpc scitex-db --to scitex-compute-04

    The dry run reports EVERY problem in one pass rather than stopping at the
    first, so you do not have to run it N times to find N problems. It answers
    in three values, never two: PASS, FAIL, and UNKNOWN. An UNKNOWN refuses just
    as firmly as a FAIL, but calls for a different action — go and measure it,
    rather than go and fix it.

    SEVERAL NAMES preflight each in turn and print a combined summary, so the
    whole shape of a queued batch is visible before any agent is touched. That
    form is read-only by construction: --no-dry-run takes exactly one name,
    because relocating several agents from one invocation would make a single
    interruption leave several agents half-moved.
    """
    if len(names) > 1 and not dry_run:
        console.print(
            "[red]refusing:[/red] --no-dry-run relocates ONE agent. A batch that is "
            "interrupted halfway leaves several agents half-moved across two hosts, "
            "and the journal that makes a relocation resumable is per-agent. Preflight "
            "them together (the default --dry-run), then move them one at a time.",
            soft_wrap=True,
        )
        raise SystemExit(2)

    results: list[Prepared] = []
    for index, name in enumerate(names):
        if index:
            console.print("")
            console.print("─" * 72)
            console.print("")
        prepared = prepare_one(name, to_host)
        results.append(prepared)
        _print_one(prepared, dry_run)

    if len(results) > 1:
        for line in _sweep_summary(results, to_host):
            console.print(line, soft_wrap=True)
        raise SystemExit(EXIT_REFUSED if any(r.blocks for r in results) else 0)

    only = results[0]
    if only.error:
        # A spec that cannot be found is a usage error (2); an agent already on
        # the target is a refusal to act, not a failure to read.
        raise SystemExit(EXIT_REFUSED if only.already_there else 2)
    assert only.report is not None
    if only.report.blocks:
        # The checks gate the executing path too, and that is the point rather
        # than a leftover: the dry run is what makes them load-bearing, and a
        # refusal that becomes advisory the moment there is something to execute
        # is not a refusal.
        raise SystemExit(EXIT_REFUSED)
    for line in _readiness_notice():
        console.print(line, soft_wrap=True)
    if dry_run:
        return

    from ._relocate_run import exit_code_for, run_relocation

    if not only.from_host:
        console.print(
            "[red]refusing to execute:[/red] the state db does not know which host "
            f"{only.name} runs on, and the spec offered nothing to seed it with. A "
            "relocation FROM an unknown host cannot stop the right source.",
            soft_wrap=True,
        )
        raise SystemExit(EXIT_REFUSED)

    outcome = run_relocation(
        name=only.name,
        spec=only.spec,
        spec_path=only.spec_path,
        from_host=only.from_host,
        to_host=to_host,
    )
    code = exit_code_for(outcome)
    if code:
        raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``relocate`` to the parent ``agents`` Click group."""
    agent_group.add_command(relocate)
