"""``sac agents migrate-layers`` — declare each spec's ``to_home`` cascade.

Step 3 of the ``to_home_layers`` migration, and the only step that writes.
Every fleet spec gains one line naming the cascade layers it ALREADY resolves,
so what an agent inherits becomes readable from its spec instead of derivable
only by re-running the resolver. Step 4 — turning the "no declaration" warning
into a refusal — is deliberately NOT here: it is only safe once this has
actually run, and shipping both together would let one command disarm the
fleet on a bad plan.

Registered onto ``sac agents`` by :func:`register`, exactly as ``reconcile``
is, and it reaches the CLI through ``_main.py``'s existing lazy ``agents``
entry — no new top-level noun. A ``spec`` group invented for one command that
becomes a permanent no-op the day it succeeds would cost more than it buys.

**Dry-run is the DEFAULT.** ``--apply`` is the deliberate act.

**The gate.** Declaring the cascade an agent already resolves cannot change
what it inherits — that is the argument, and an argument is not a measurement.
So the apply measures what every agent ARMS before writing, re-measures after,
and rolls every spec back unless the two are identical over the whole
population (:mod:`.._maintenance._layers_migration_gate`). An agent it could
not measure on either side blocks the sweep rather than being skipped.

**Exit codes**, and the distinction that drives them:

  0  the plan is sound (refusals included), or the apply was verified
  1  a spec is MALFORMED or UNREADABLE — the plan does not describe the sweep
  2  the apply was refused or rolled back by the arming gate

A REFUSAL is not a failure. "This spec has no ``to_home:`` line to anchor to"
is an expected outcome that a human resolves; exiting non-zero on it would
train every reader to ignore the exit code of a command whose whole job is to
be trusted about what it did.
"""

from __future__ import annotations

import datetime as _dt
import json

import click
from rich.markup import escape

from .._maintenance._layers_migration_apply import apply_migration
from .._maintenance._layers_migration_gate import fleet_arming_snapshot, gate_arming
from .._maintenance._layers_migration_plan import (
    already_declared,
    fleet_spec_paths,
    plan_migration,
    quiet_undeclared_warning,
)
from ._helpers import _json_flag, console

_EXIT_OK = 0
_EXIT_PLAN_UNSOUND = 1
_EXIT_GATE_REFUSED = 2


# Every dynamic value printed through `console` goes through rich's markup
# escaper. This is not defensive habit — MEASURED: the layer list renders as
# `[user-shared]`, which rich parses as a style tag and SWALLOWS, so the first
# run of this command reported a correct histogram with all 101 layer sets
# blank. A report whose most important line silently empties itself is worse
# than one that crashes.
def _lit(text: str) -> str:
    return escape(str(text))


def _archive_dir():
    """Where originals are parked before the first byte changes."""
    from .._runtime_paths import runtime_base_dir

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return runtime_base_dir() / "layers-migration" / stamp


def _layer_histogram(edits) -> "dict[str, int]":
    """How many specs resolve each distinct layer set, most common first.

    The single most useful number in a dry-run: it says what the sweep would
    actually declare across the fleet, which a per-spec list of 102 lines
    hides rather than shows.
    """
    hist: dict[str, int] = {}
    for edit in edits:
        key = ", ".join(edit.layers) or "(none)"
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def _plan_payload(plan) -> dict:
    """Everything the plan knows, with every key present on every call."""
    return {
        "specs": len(plan.edits) + len(plan.unreadable),
        "writable": len(plan.writable),
        "refused": [
            {"agent": e.agent, "reason": e.refusal, "path": str(e.path)}
            for e in plan.refused
        ],
        "already_declared": [e.agent for e in already_declared(plan)],
        "malformed": [
            {"agent": e.agent, "lines_added": e.lines_added} for e in plan.malformed
        ],
        "unreadable": list(plan.unreadable),
        "layer_sets": _layer_histogram(plan.writable),
        "safe_to_apply": plan.safe_to_apply,
        "summary": plan.summary(),
    }


def _render_plan(plan, *, verbose: bool) -> None:
    payload = _plan_payload(plan)
    console.print(
        f"[bold]{payload['specs']} spec(s)[/bold] — "
        f"{payload['writable']} would gain a to_home_layers declaration\n"
    )
    for layers, count in payload["layer_sets"].items():
        console.print(f"  [green]{count:4d}[/green]  {_lit('[' + layers + ']')}")
    if payload["already_declared"]:
        console.print(
            f"\n[dim]{len(payload['already_declared'])} spec(s) already declare "
            f"the key — nothing to do for those.[/dim]"
        )
    # Never silent about a decision: refusals print whether or not -v is given.
    for entry in payload["refused"]:
        console.print(
            f"\n[yellow]REFUSED[/yellow] {_lit(entry['agent'])}: "
            f"{_lit(entry['reason'])}",
            soft_wrap=True,
        )
        console.print(f"    [dim]{_lit(entry['path'])}[/dim]", soft_wrap=True)
    for entry in payload["malformed"]:
        console.print(
            f"\n[red]MALFORMED[/red] {_lit(entry['agent'])}: the planned edit "
            f"touches {entry['lines_added']} line(s), not 1 — this is a DEFECT "
            f"in the editor, not a spec needing attention",
            soft_wrap=True,
        )
    for entry in payload["unreadable"]:
        console.print(f"\n[red]UNREADABLE[/red] {_lit(entry)}", soft_wrap=True)
    if verbose:
        console.print("")
        for edit in plan.writable:
            layers = _lit("[" + ", ".join(edit.layers) + "]")
            console.print(
                f"  [dim]{_lit(edit.agent)}[/dim] -> {layers}", soft_wrap=True
            )
    console.print(f"\n[bold]{_lit(payload['summary'])}[/bold]")


def _run_apply(plan, covered) -> "tuple[int, dict]":
    """Snapshot, write, re-snapshot, and undo unless the arming is identical."""
    before = fleet_arming_snapshot(covered)
    if before.unmeasurable:
        # Refuse BEFORE writing. The gate would catch this after the fact too,
        # but writing 100 files to learn something knowable beforehand is not a
        # safety property, it is a rollback waiting to be needed.
        return _EXIT_GATE_REFUSED, {
            "mode": "apply",
            "written": [],
            # `apply_refused`, NOT `refused`: the plan payload already owns
            # `refused` as the per-spec list of specs the EDITOR declined.
            # Reusing that key would make one JSON field mean a list of specs
            # in dry-run and a sentence in apply mode, which is how a consumer
            # ends up reading "the sweep refused nothing" off a refusal.
            "apply_refused": (
                f"{len(before.unmeasurable)} agent(s) could not be measured "
                f"BEFORE the sweep, so no post-write comparison could prove "
                f"them unchanged"
            ),
            "before_unmeasurable": list(before.unmeasurable),
        }

    expected = len(covered)
    verdicts: list = []

    def _verify():
        after = fleet_arming_snapshot(covered)
        verdict = gate_arming(before, after, expected=expected)
        verdicts.append(verdict)
        return verdict

    result = apply_migration(plan, _archive_dir(), _verify)
    gate = verdicts[-1].to_dict() if verdicts else None
    payload = {
        "mode": "apply",
        "written": list(result.written),
        "archive_dir": str(result.archive_dir) if result.archive_dir else None,
        "apply_refused": result.refused,
        "rolled_back": result.rolled_back,
        "applied": result.applied,
        "gate": gate,
    }
    if result.applied:
        return _EXIT_OK, payload
    return _EXIT_GATE_REFUSED, payload


def _render_apply(payload: dict) -> None:
    if payload.get("applied"):
        console.print(
            f"[green]APPLIED[/green] {len(payload['written'])} spec(s) written "
            f"and verified.\n  [dim]originals archived at "
            f"{_lit(payload['archive_dir'])}[/dim]"
        )
        console.print(f"  [dim]{_lit(payload['gate']['summary'])}[/dim]")
        return
    if payload.get("rolled_back"):
        console.print(
            f"[red]ROLLED BACK[/red] — the specs were written, the arming gate "
            f"refused them, and every original was restored.\n"
            f"  {_lit(payload['rolled_back'])}"
        )
        return
    console.print(
        f"[red]REFUSED[/red] — nothing was written.\n  {_lit(payload['apply_refused'])}"
    )
    for entry in payload.get("before_unmeasurable", []):
        console.print(
            f"    [magenta]UNMEASURABLE[/magenta] {_lit(entry)}", soft_wrap=True
        )


@click.command(name="migrate-layers")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY write the declarations. Without this, nothing is written.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report only, write nothing. This is already the DEFAULT.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    help="Also list every spec that would be written, not just the counts.",
)
@click.pass_context
def migrate_layers(
    ctx: click.Context,
    apply: bool,
    dry_run: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """Declare each spec's ``to_home`` cascade in the spec. Dry-run by default.

    Writes ONE line per spec — ``to_home_layers: [...]`` naming the layers that
    spec already resolves. Behaviour-preserving by construction and verified by
    measurement: the apply compares what every agent ARMS before and after, and
    restores every original unless the two are identical.

    \b
    Preview (read-only — the DEFAULT):
      $ sac agents migrate-layers
      $ sac agents migrate-layers --json
    \b
    Write:
      $ sac agents migrate-layers --apply

    Exits 0 when the plan is sound (a named REFUSAL is not a failure), 1 when a
    spec is malformed or unreadable, 2 when the apply was refused or rolled
    back by the arming gate.
    """
    if apply and dry_run:
        raise click.UsageError(
            "--apply and --dry-run are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to write."
        )

    # Quieted around the resolver calls only: the "declares no layers" WARNING
    # fires once per undeclared spec (101 on this host) and this command's whole
    # output IS that finding, aggregated. See quiet_undeclared_warning.
    with quiet_undeclared_warning():
        plan = plan_migration(fleet_spec_paths())
    payload = _plan_payload(plan)
    payload["mode"] = "apply" if apply else "dry-run"

    if not apply:
        code = _EXIT_OK if plan.safe_to_apply else _EXIT_PLAN_UNSOUND
        payload["exit_code"] = code
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(code)
        console.print("[bold]sac agents migrate-layers[/bold]  dry-run (read-only)\n")
        _render_plan(plan, verbose=verbose)
        if plan.writable:
            console.print(
                "\nNothing was written — this is a dry-run. To act:\n"
                "    sac agents migrate-layers --apply"
            )
        raise SystemExit(code)

    if not plan.safe_to_apply:
        payload["exit_code"] = _EXIT_PLAN_UNSOUND
        payload["apply_refused"] = f"plan is not safe to apply: {plan.summary()}"
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(_EXIT_PLAN_UNSOUND)
        console.print("[bold]sac agents migrate-layers[/bold]  apply\n")
        _render_plan(plan, verbose=verbose)
        console.print(
            "\n[red]REFUSED[/red] — nothing was written. A plan that cannot "
            "describe every spec does not describe the sweep."
        )
        raise SystemExit(_EXIT_PLAN_UNSOUND)

    if not plan.writable:
        payload["exit_code"] = _EXIT_OK
        payload["written"] = []
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(_EXIT_OK)
        console.print(
            "[green]Nothing to write[/green] — every spec already declares its "
            "layers. The sweep is idempotent; this is what a completed one "
            "looks like."
        )
        raise SystemExit(_EXIT_OK)

    with quiet_undeclared_warning():
        code, applied = _run_apply(plan, [e.path for e in plan.edits])
    payload.update(applied)
    payload["exit_code"] = code
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)
    console.print("[bold]sac agents migrate-layers[/bold]  apply\n")
    _render_apply(payload)
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``migrate-layers`` to the parent ``agents`` Click group."""
    agent_group.add_command(migrate_layers)


__all__ = ["migrate_layers", "register"]
