"""What ``sac agents migrate-engines`` PRINTS, and the payload behind it.

Split out of the command module, which had outgrown its line budget. The
command decides and writes; this decides what a human and a scheduled runner
are told, and those are the two places this sweep has been wrong in ways no
exit code showed:

* an apply that wrote nothing printed "this is what a completed one looks
  like" over a fleet where every spec had been REFUSED, and counted the
  SELECTED specs as proof. ``migration_complete`` is now the claim, and it
  is about the migration rather than about this invocation;
* the human path carried the refusals only in ``--json``, so the readable
  output was clean over a fleet it could not migrate;
* the success line never named the root it had written into — and the
  default root is the untracked live copy, which inside a container is not
  even the host's.

THE SAME SHAPE, THREE MORE TIMES, and all three are about the ``--json``
payload carrying a fact the readable output did not:

* a ``--agent``/``--host`` filter narrowed the census and left no trace of
  itself anywhere, so the completion sentence above came back — this time
  over a run that had covered 1 of 113 specs and NAMED the full root;
* ``--host-supports-engines`` lifted the floor for a host the roster records
  as a MEASURED negative and printed nothing at all: ``grep -i spartan`` over
  the whole terminal output matched zero lines while nine specs moved from
  REFUSED into the migratable count. An override of a MEASURED NO is
  categorically different from an override of an UNKNOWN, and it has to
  restate the measurement it contradicts;
* the floor's evidence reached the report only on the REFUSING side, so 100
  approvals said nothing about which hosts were judged capable, on what, or
  how old the measurement is.

AND ONE ABOUT VOLUME. The floor's refusal detail is per-HOST and ~400
characters, and ``render_plan`` printed it once per SPEC: twelve refusals
rendered as twelve blocks, nine of them the byte-identical spartan paragraph.
:func:`_group_refusals` is what makes the "one block per KIND" contract that
``_engines_floor``'s own comment claims actually true of the human output and
not only of the payload.
"""

from __future__ import annotations

from rich.markup import escape

from ._helpers import console

__all__ = [
    "plan_payload",
    "render_apply",
    "render_diffs",
    "render_floor",
    "render_plan",
]


def _lit(text: str) -> str:
    """Escape before printing through rich.

    Not habit — MEASURED on the sibling sweep: a value rendering as
    ``[user-shared]`` is parsed by rich as a style tag and SWALLOWED, so a
    report printed a correct histogram with every row blank. Engine keys and
    model ids here include ``opus[1m]``, which is exactly that shape.
    """
    return escape(str(text))


def plan_payload(plan, root, *, floor=None, excluded_roots=()) -> dict:
    """``root`` is one root or several — the report names every one of them.

    The default sweep searches EVERY user-scope root, so a payload carrying
    the first of them would be a claim about a directory the sweep did not
    confine itself to. ``roots`` is every root RESOLVED, in order;
    ``roots_absent`` is the subset that is not a directory and
    ``roots_excluded`` the subset deliberately not swept — both named rather
    than quietly dropped, because an operator whose tree was resolved and then
    left out has to be able to see that. It is the same distinction
    :mod:`..._maintenance._roster_state` draws between "empty" and "absent".
    ``root`` stays the human line and names them all.

    ``floor`` supplies the ``engine_floor`` block: which hosts were consulted,
    the measured row each was judged by, and how many specs each covers.
    Passing None omits it — the CLI always passes one.
    """
    from pathlib import Path

    given = list(root) if isinstance(root, (list, tuple)) else [root]
    roots = [str(r) for r in given]
    absent = [str(r) for r in given if not Path(r).is_dir()]
    payload = {
        "root": ", ".join(roots),
        "roots": roots,
        "roots_absent": absent,
        "roots_excluded": [str(r) for r in excluded_roots],
        "roster": plan.roster.state if plan.roster else None,
        "specs": len(plan.outcomes),
        # The narrowing flags, echoed. A filtered run is a census of a SUBSET
        # and nothing said so, so `migration_complete` went true over 1 of 113.
        "selectors": list(plan.selectors),
        "filtered": bool(plan.selectors),
        # A selector that matched nothing: a typo or a renamed agent used to
        # drop out of every batch in silence, exit 0, forever.
        "unmatched_agents": list(plan.unmatched_agents),
        "unmatched_hosts": list(plan.unmatched_hosts),
        "would_migrate": len(plan.migrated),
        # PATHS, not just the count: `written` carries agent names, and a name
        # cannot say which root a write landed in.
        "would_migrate_paths": [str(o.path) for o in plan.migrated],
        "already_migrated": [o.agent for o in plan.already],
        # Migratable, and past --limit. Named so a batch is never mistaken
        # for a completed sweep — see ``migration_complete`` below.
        "held_back": [o.agent for o in plan.held_back],
        "refused": [
            {"agent": o.agent, "reason": o.reason, "detail": o.detail}
            for o in plan.refused
        ],
        "unreadable": [{"agent": o.agent, "detail": o.detail} for o in plan.unreadable],
        "skipped_templates": list(plan.skipped_templates),
        # A spec.yaml an earlier root's copy displaced. It is on disk, it is
        # legacy, and earlier-root-wins is deterministic, so no later run
        # reaches it: the count must say where the difference went.
        "shadowed": [
            {"agent": s.agent, "kept": str(s.kept), "shadowed": str(s.dropped)}
            for s in plan.shadowed
        ],
        "engine_sets": _engine_histogram(plan),
        "safe_to_apply": plan.safe_to_apply,
        # THE QUESTION A SCHEDULED RUNNER IS ACTUALLY ASKING, and neither
        # ``exit_code`` nor ``applied`` answers it: both are 0/true for a run
        # that wrote nothing because every spec was refused or held back.
        "migration_complete": plan.is_complete,
        # The same answer in prose, from the same source, so the two cannot
        # disagree about WHY the migration is unfinished.
        "outstanding": list(plan.outstanding),
        "summary": plan.summary(),
    }
    if floor is not None:
        from .._maintenance._engines_floor_audit import floor_audit

        payload["engine_floor"] = floor_audit(
            floor, [o.hosts if o.hosts is None else set(o.hosts) for o in plan.outcomes]
        ).as_dict()
    return payload


def render_floor(audit: "dict | None") -> None:
    """The BASIS for every write this run would make, and every lift of it.

    Two things were invisible in the readable output, and both of them are
    about the floor being trusted rather than read:

    * the approvals carried no evidence. ``HOST_SUPPORT`` is a static table
      with no expiry and nothing probes, so a row that goes stale — a host
      rebuilt or rolled back onto an older sac after its ``measured_on`` date
      — makes a wrong run byte-for-byte identical to a right one. Printing
      the rows consulted, with their dates, is what makes it arguable;
    * ``--host-supports-engines`` printed NOTHING. Measured: the run went
      from "100 would be migrated; 12 REFUSED" to "109 would be migrated; 3
      REFUSED" and ``grep -in 'spartan|override|lift'`` over the full output
      matched zero lines. The nine specs simply appeared in the migratable
      count with the reader never seeing the fact being overridden.
    """
    if not audit or not audit.get("active"):
        return
    dates = ", ".join(audit["measured_on"]) or "no measured rows"
    console.print(
        f"[bold]version floor[/bold] {len(audit['hosts'])} host(s) consulted, "
        f"roster measured {_lit(dates)}",
        soft_wrap=True,
    )
    width = max((len(r["host"]) for r in audit["hosts"]), default=0)
    for row in audit["hosts"]:
        colour = "green" if row["support"] == "supports-engines" else "yellow"
        when = (
            f"measured {row['measured_on']}"
            if row["measured_on"]
            else "absent from the roster"
        )
        # The alias is a MEASURED fact, not a courtesy: ywata-note-win answers
        # `hostname` on both ssh names, so 14 specs spelling it the retired way
        # are judged by the scitex-laptop-01 row and the reader must see that.
        alias = (
            f"  (= {_lit(row['canonical'])})"
            if row["canonical"] != row["host"]
            else ""
        )
        console.print(
            f"  [{colour}]{row['specs']:4d}[/{colour}]  "
            f"{_lit(row['host'].ljust(width))}  {_lit(row['support'].ljust(16))}  "
            f"{_lit(when)}{alias}{'  LIFTED' if row['overridden'] else ''}",
            soft_wrap=True,
        )
    if audit["specs_with_an_unreadable_host"]:
        console.print(
            f"  [red]{audit['specs_with_an_unreadable_host']:4d}[/red]  "
            f"(host unreadable)",
            soft_wrap=True,
        )
    if audit["specs_with_no_declared_host"]:
        console.print(
            f"  [yellow]{audit['specs_with_no_declared_host']:4d}[/yellow]  "
            f"(spec names no host)",
            soft_wrap=True,
        )
    _render_overrides(audit)


def _render_overrides(audit: dict) -> None:
    """Say what each ``--host-supports-engines`` claim actually overrode.

    A lift of an UNKNOWN says "nobody looked, I did". A lift of a MEASURED
    negative says "the roster looked and I disagree" — and by the roster's own
    evidence those specs then fail their host's validator and stop those
    agents starting. Two different claims; only one of them can strand an
    agent, so only one gets the red block restating the measurement.
    """
    for row in (r for r in audit["hosts"] if r["overridden"]):
        if row["contradicts_a_measurement"]:
            console.print(
                f"\n[red]FLOOR LIFTED[/red] --host-supports-engines "
                f"{_lit(row['host'])} CONTRADICTS a measurement: "
                f"{_lit(row['host'])} is recorded {_lit(row['support'])}, "
                f"measured {_lit(row['measured_on'])} — {_lit(row['evidence'])}. "
                f"{row['specs']} spec(s) pinned there will be written and, by "
                f"that measurement, then fail their host's validator and stop "
                f"those agents starting.",
                soft_wrap=True,
            )
            continue
        console.print(
            f"\n[yellow]FLOOR LIFTED[/yellow] --host-supports-engines "
            f"{_lit(row['host'])}: nobody measured that host, so nothing here "
            f"contradicts it — {row['specs']} spec(s) will be written on your "
            f"claim.",
            soft_wrap=True,
        )


def _engine_histogram(plan) -> "dict[str, int]":
    hist: dict[str, int] = {}
    for outcome in plan.migrated:
        key = ", ".join(outcome.engine_keys)
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def _group_refusals(entries) -> "list[tuple[str, str, list[str]]]":
    """``(reason, detail, agents)`` — one entry per KIND, not per spec.

    The refusal CONSTANTS are constant precisely so a 119-spec sweep can do
    this, and the floor's detail is per-HOST, so ``(reason, detail)`` is the
    key that makes the contract true. Rendering per-spec instead printed the
    identical ~400-character spartan paragraph nine times inside twelve
    refusal blocks, and would have printed it sixty times for a whole-host
    refusal on ``scitex-laptop-01`` — burying the review an operator does
    before a 100-file apply under copies of two paragraphs.
    """
    grouped: "dict[tuple[str, str], list[str]]" = {}
    for entry in entries:
        grouped.setdefault((entry["reason"], entry["detail"]), []).append(
            entry["agent"]
        )
    return [(reason, detail, agents) for (reason, detail), agents in grouped.items()]


def _render_selection_gaps(payload: dict) -> None:
    """Everything the SELECTION left out, named. Never a silent narrowing."""
    if payload.get("selectors"):
        console.print(
            f"\n[cyan]FILTERED[/cyan] this run was narrowed to "
            f"{_lit(', '.join(payload['selectors']))} — it is a census of a "
            f"SUBSET, not of the roster, so it can never report the migration "
            f"as complete.",
            soft_wrap=True,
        )
    for kind, key in (("--agent", "unmatched_agents"), ("--host", "unmatched_hosts")):
        if payload.get(key):
            console.print(
                f"\n[yellow]MATCHED NOTHING[/yellow] {kind} "
                f"{_lit(', '.join(payload[key]))} selected no spec. A typo or a "
                f"renamed agent drops out of every batch in silence otherwise.",
                soft_wrap=True,
            )
    for entry in payload.get("shadowed", ()):
        console.print(
            f"\n[yellow]SHADOWED[/yellow] {_lit(entry['agent'])}: "
            f"{_lit(entry['shadowed'])} was NOT examined — an earlier root "
            f"supplied {_lit(entry['kept'])} for the same agent name. Earlier "
            f"roots win deterministically, so no later run reaches it either; "
            f"the loader raises AmbiguousRegistryScope on this collision and a "
            f"human has to resolve it.",
            soft_wrap=True,
        )


def render_plan(plan, payload: dict, *, diff: bool) -> None:
    if plan.roster is not None and not plan.roster.is_populated:
        console.print(f"[red]NO ROSTER SEARCHED[/red] — {_lit(plan.roster.describe())}")
        return
    console.print(
        f"[bold]{payload['specs']} spec(s)[/bold] under {_lit(payload['root'])} — "
        f"{payload['would_migrate']} would gain a spec.engines block\n"
    )
    for keys, count in payload["engine_sets"].items():
        console.print(f"  [green]{count:4d}[/green]  engines: {_lit(keys)}")
    if payload.get("roots_absent"):
        # A resolved root that is not there was NOT searched. Dropping it from
        # the report would let an operator whose tree is missing read the
        # count as covering it.
        console.print(
            f"[yellow]NOT SEARCHED[/yellow] "
            f"{len(payload['roots_absent'])} resolved root(s) do not exist "
            f"({_lit(', '.join(payload['roots_absent']))}).\n",
            soft_wrap=True,
        )
    if payload.get("roots_excluded"):
        console.print(
            f"[yellow]NOT SEARCHED[/yellow] "
            f"{len(payload['roots_excluded'])} project-local root(s) found by "
            f"walking up from the working directory "
            f"({_lit(', '.join(payload['roots_excluded']))}). A repo's own "
            f"checked-in fixtures are not the fleet, and a sweep whose scope "
            f"changed with the cwd could rewrite them. Pass --root to sweep "
            f"one deliberately.\n",
            soft_wrap=True,
        )
    render_floor(payload.get("engine_floor"))
    _render_selection_gaps(payload)
    if payload["already_migrated"]:
        console.print(
            f"\n[dim]{len(payload['already_migrated'])} spec(s) already declare "
            f"spec.engines — nothing to do for those.[/dim]"
        )
    for reason, detail, agents in _group_refusals(payload["refused"]):
        console.print(
            f"\n[yellow]REFUSED[/yellow] {len(agents)} spec(s): {_lit(reason)}\n"
            f"    {_lit(', '.join(sorted(agents)))}",
            soft_wrap=True,
        )
        if detail:
            console.print(f"    [dim]{_lit(detail)}[/dim]", soft_wrap=True)
    for entry in payload["unreadable"]:
        console.print(
            f"\n[red]UNREADABLE[/red] {_lit(entry['agent'])}: {_lit(entry['detail'])}",
            soft_wrap=True,
        )
    if payload["held_back"]:
        console.print(
            f"\n[cyan]HELD BACK[/cyan] {len(payload['held_back'])} spec(s) past "
            f"--limit ({_lit(', '.join(payload['held_back']))}). Run the same "
            f"command again to take the next batch.",
            soft_wrap=True,
        )
    if payload["skipped_templates"]:
        # Named, never silent: `sac agents create` copies these, so a template
        # left behind re-introduces the legacy shape on every agent made after
        # the sweep — the migration would then never finish.
        console.print(
            f"\n[yellow]NOT SEARCHED[/yellow] "
            f"{len(payload['skipped_templates'])} template spec(s) "
            f"({_lit(', '.join(payload['skipped_templates']))}). "
            f"`sac agents create` copies them, so an unmigrated template "
            f"re-introduces the legacy shape on every new agent. Pass "
            f"--templates to include them.",
            soft_wrap=True,
        )
    if diff:
        render_diffs(plan)
    console.print(f"\n[bold]{_lit(payload['summary'])}[/bold]")


def _render_unfinished(payload: dict) -> None:
    """Name everything a further run still has to do. Never a bare count.

    Sourced from ``plan.outstanding``, which is the SAME list
    ``migration_complete`` is the emptiness of — so the sentence a human reads
    and the boolean a scheduled runner reads cannot say different things.
    Re-deriving it here is how the ``--agent`` filter and the skipped
    templates came to be reported in one and absent from the other.
    """
    for line in payload.get("outstanding", ()):
        console.print(f"  [yellow]•[/yellow] {_lit(line)}", soft_wrap=True)
    for reason, _detail, agents in _group_refusals(payload["refused"]):
        console.print(
            f"  [yellow]REFUSED[/yellow] {_lit(', '.join(sorted(agents)))}: "
            f"{_lit(reason)}",
            soft_wrap=True,
        )
    if payload["held_back"]:
        console.print(
            f"  [cyan]HELD BACK[/cyan] {len(payload['held_back'])} past --limit "
            f"({_lit(', '.join(payload['held_back']))})",
            soft_wrap=True,
        )


def render_apply(result, payload: dict) -> None:
    if result.applied and not result.written:
        # THE CLAIM IS ABOUT THE MIGRATION, so it is made only when the
        # migration is finished. "Nothing was written" is true of a completed
        # sweep AND of a run whose every spec was refused, and of a --limit
        # batch that already took its N — and the earlier form of this
        # sentence counted the SELECTED specs, so it called both of those
        # completed. A scheduled runner reading exit 0 believed it.
        if not payload["migration_complete"]:
            console.print(
                f"[yellow]Nothing was written[/yellow] — "
                f"{len(payload['already_migrated'])} of {payload['specs']} "
                f"spec(s) under {_lit(payload['root'])} declare spec.engines. "
                f"The sweep is NOT complete:",
                soft_wrap=True,
            )
            _render_unfinished(payload)
            return
        console.print(
            f"[green]Nothing to write[/green] — all "
            f"{payload['specs']} spec(s) under {_lit(payload['root'])} already "
            f"declare spec.engines. The sweep is idempotent; this is what a "
            f"completed one looks like."
        )
        return
    if result.applied:
        console.print(
            f"[green]APPLIED[/green] {len(result.written)} spec(s) written and "
            f"verified under {_lit(payload['root'])} — every one still resolves "
            f"the SAME backend.\n"
            f"  [dim]originals archived at {_lit(result.archive_dir)}[/dim]"
        )
        if not payload["migration_complete"]:
            console.print("\n[bold]Still outstanding[/bold] — run again:")
            _render_unfinished(payload)
        return
    if result.rolled_back:
        console.print(
            f"[red]ROLLED BACK[/red] — {_lit(result.rolled_back)}", soft_wrap=True
        )
        for entry in (*result.drift, *result.errors):
            console.print(f"    [magenta]{_lit(entry)}[/magenta]", soft_wrap=True)
        return
    console.print(
        f"[red]REFUSED[/red] — nothing was written.\n  {_lit(result.refused)}",
        soft_wrap=True,
    )


def render_diffs(plan) -> None:
    """One unified diff per spec about to be written.

    Used by BOTH the dry-run and the apply. ``--diff`` is on by default and
    its own help calls it "the whole point"; the apply path used to accept
    the flag and print nothing, so an operator who believed they were
    reviewing the rewrite saw a one-line summary and no diff at all.
    """
    for outcome in plan.migrated:
        console.print(f"\n[bold]{_lit(outcome.agent)}[/bold]")
        console.print(_lit(outcome.diff), soft_wrap=True, highlight=False)
