"""``sac agents scratch-migrate`` — move overlay ``uvwork`` onto scratch.

The operator-facing half of the ADR-0024 migration. What an agent had
accumulated under ``/uvwork`` before the scratch bind existed sits in
``overlays/<agent>/upper/uvwork`` on the root LV; this verb moves it to
``<scratch_root>/sac/agents/<agent>/uvwork``, where the bind now looks, so
the next start finds uv and the venv in place and the root LV gets its
space back (11.7 GB for sac alone, measured 2026-09-03).

**Dry-run is the DEFAULT.** It prints, per agent, the overlay copy's size
and the decision, then the total it would move. ``--apply`` is the deliberate
act, and even then only a provably STOPPED agent is touched: a running one
is refused BY NAME (its container has the overlay mounted), as is one whose
liveness the runtime adapter could not determine.

Registered onto ``sac agents`` by :func:`register`, exactly as
``migrate-layers`` is.

**Exit codes**:

  0  the plan is sound and (with ``--apply``) every selected move verified
  1  the plan does not describe the sweep — no roster was searched, a spec
     is unreadable, or a ``--agent`` names nobody
  2  refused: the host keeps ``/uvwork`` in the overlay (``scratch_root:
     none``), a selected agent is running / undeterminable / already
     migrated, or a move failed verification (its overlay copy is KEPT)
"""

from __future__ import annotations

import json

import click
from rich.markup import escape

from .._maintenance._scratch_migrate import (
    ScratchPlan,
    apply_scratch_migration,
    liveness_vantage,
    plan_scratch_migration,
)
from .._state.host_scratch import ScratchRootError, resolve_scratch_root
from ._helpers import _json_flag, console

_EXIT_OK = 0
_EXIT_PLAN_UNSOUND = 1
_EXIT_REFUSED = 2


def _lit(text) -> str:
    """Every dynamic value goes through rich's escaper — a path or reason
    containing ``[...]`` would otherwise be parsed as a style tag."""
    return escape(str(text))


def human_bytes(n: int) -> str:
    """``11.7 GiB`` / ``512.0 MiB`` / ``3 B`` — the unit the operator measured in."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"  # pragma: no cover — the loop above always returns


def _payload(plan: ScratchPlan, *, mode: str) -> dict:
    return {
        "mode": mode,
        "scratch_root": str(plan.scratch.root),
        "scratch_source": plan.scratch.source,
        "root": str(plan.roster.root),
        "roster": plan.roster.state,
        "specs": len(plan.rows) + len(plan.unreadable),
        "rows": [r.to_dict() for r in plan.rows],
        "would_move": [r.agent for r in plan.movable],
        "refused": [{"agent": r.agent, "reason": r.reason} for r in plan.refused],
        "unknown": list(plan.unknown),
        "unreadable": list(plan.unreadable),
        "total_bytes": plan.total_bytes,
        "total_human": human_bytes(plan.total_bytes),
        "safe_to_apply": plan.safe_to_apply,
        # Empty string = host pids are resolvable here, so "stopped" may be
        # believed. Non-empty = every row is UNKNOWN and nothing can move;
        # see _scratch_migrate_liveness.liveness_vantage.
        "liveness_vantage": liveness_vantage(),
        "summary": plan.summary(),
    }


def _render_plan(plan: ScratchPlan) -> None:
    if not plan.roster.is_populated:
        console.print(f"[red]NO ROSTER SEARCHED[/red] — {_lit(plan.roster.describe())}")
        return
    console.print(
        f"scratch root: [bold]{_lit(plan.scratch.root)}[/bold] "
        f"[dim]({_lit(plan.scratch.source)}: {_lit(plan.scratch.reason)})[/dim]"
    )
    # Said ONCE at the top rather than only inside 17 identical row reasons:
    # from a blind vantage nothing can move, and that is the headline.
    blind = liveness_vantage()
    if blind:
        console.print(
            f"[yellow]LIVENESS UNREADABLE FROM HERE[/yellow] — {_lit(blind)}\n"
            "Sizes below are real (the overlays are read through a bind "
            "mount); every agent is refused because no agent can be shown "
            "to be stopped.",
            soft_wrap=True,
        )
    console.print(
        f"[bold]{len(plan.rows) + len(plan.unreadable)} spec(s)[/bold] under "
        f"{_lit(plan.roster.root)}\n"
    )
    for row in plan.rows:
        if row.action == "move":
            tag = "[green]MOVE   [/green]"
        elif row.action == "refuse":
            tag = "[yellow]REFUSED[/yellow]"
        else:
            tag = "[dim]nothing[/dim]"
        size = human_bytes(row.bytes) if row.source is not None else "-"
        console.print(
            f"  {tag} {_lit(row.agent):<32} {_lit(size):>10}  {_lit(row.reason)}",
            soft_wrap=True,
        )
    for name in plan.unknown:
        console.print(f"\n[red]UNKNOWN[/red] --agent {_lit(name)}: no such spec under {_lit(plan.roster.root)}")
    for entry in plan.unreadable:
        console.print(f"\n[red]UNREADABLE[/red] {_lit(entry)}", soft_wrap=True)
    console.print(
        f"\n[bold]{len(plan.movable)} agent(s), {human_bytes(plan.total_bytes)} "
        f"({plan.total_bytes} bytes) would move from the overlay upper to "
        f"{_lit(plan.scratch.root)}[/bold]"
    )
    if plan.refused:
        console.print(
            f"[yellow]{len(plan.refused)} refused[/yellow]: "
            + ", ".join(_lit(r.agent) for r in plan.refused)
        )


def _render_apply(results) -> None:
    for res in results:
        tag = "[green]MOVED [/green]" if res.moved else "[red]FAILED[/red]"
        console.print(f"  {tag} {_lit(res.agent):<32} {_lit(res.detail)}", soft_wrap=True)
    moved = [r for r in results if r.moved]
    console.print(
        f"\n[bold]{len(moved)}/{len(results)} moved, "
        f"{human_bytes(sum(r.bytes for r in moved))} freed from the overlay upper[/bold]"
    )


@click.command(name="scratch-migrate")
@click.option(
    "--agent",
    "agents",
    multiple=True,
    metavar="NAME",
    help="Only this agent (repeatable). Default: every spec in the fleet roster.",
)
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY move the trees. Without this, nothing is written.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def scratch_migrate(ctx: click.Context, agents: tuple[str, ...], apply: bool, as_json: bool) -> None:
    """Move each STOPPED agent's overlay /uvwork onto the host scratch volume.

    Copies ``overlays/<agent>/upper/uvwork`` to
    ``<scratch_root>/sac/agents/<agent>/uvwork`` (where sac now binds /uvwork
    from), verifies every path and byte count, then removes the overlay copy.
    Dry-run by default; running agents are refused by name.

    \b
    Preview (read-only — the DEFAULT):
      $ sac agents scratch-migrate
      $ sac agents scratch-migrate --agent sac --json
    \b
    Move:
      $ sac agents scratch-migrate --apply
    """
    want_json = _json_flag(ctx, as_json)
    mode = "apply" if apply else "dry-run"

    try:
        scratch = resolve_scratch_root()
    except ScratchRootError as exc:
        _emit_refusal(want_json, mode, f"no scratch root: {exc}")
        raise SystemExit(_EXIT_REFUSED)
    if scratch.root is None:
        _emit_refusal(
            want_json,
            mode,
            f"this host keeps /uvwork in the overlay (scratch_root: none — "
            f"{scratch.reason}); there is nowhere to migrate to",
        )
        raise SystemExit(_EXIT_REFUSED)

    plan = plan_scratch_migration(scratch, only=list(agents))
    payload = _payload(plan, mode=mode)

    if not apply:
        code = _EXIT_OK if plan.safe_to_apply else _EXIT_PLAN_UNSOUND
        payload["exit_code"] = code
        if want_json:
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(code)
        console.print("[bold]sac agents scratch-migrate[/bold]  dry-run (read-only)\n")
        _render_plan(plan)
        if plan.movable:
            console.print(
                "\nNothing was moved — this is a dry-run. To act:\n"
                "    sac agents scratch-migrate --apply"
            )
        raise SystemExit(code)

    if not plan.safe_to_apply:
        payload["exit_code"] = _EXIT_PLAN_UNSOUND
        payload["apply_refused"] = f"plan is not safe to apply: {plan.summary()}"
        if want_json:
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(_EXIT_PLAN_UNSOUND)
        console.print("[bold]sac agents scratch-migrate[/bold]  apply\n")
        _render_plan(plan)
        console.print(
            "\n[red]REFUSED[/red] — nothing was moved. A plan that cannot "
            "describe every selected spec does not describe the sweep."
        )
        raise SystemExit(_EXIT_PLAN_UNSOUND)

    results = apply_scratch_migration(plan)
    failed = [r for r in results if not r.moved]
    code = _EXIT_REFUSED if (plan.refused or failed) else _EXIT_OK
    payload["exit_code"] = code
    payload["results"] = [r.to_dict() for r in results]
    payload["moved"] = [r.agent for r in results if r.moved]
    payload["failed"] = [r.to_dict() for r in failed]
    if want_json:
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)
    console.print("[bold]sac agents scratch-migrate[/bold]  apply\n")
    _render_plan(plan)
    console.print("")
    _render_apply(results)
    raise SystemExit(code)


def _emit_refusal(want_json: bool, mode: str, reason: str) -> None:
    if want_json:
        click.echo(
            json.dumps(
                {"mode": mode, "apply_refused": reason, "exit_code": _EXIT_REFUSED},
                indent=2,
            )
        )
        return
    console.print(f"[red]REFUSED[/red] — {_lit(reason)}", soft_wrap=True)


def register(agent_group) -> None:
    """Attach ``scratch-migrate`` to the parent ``agents`` Click group."""
    agent_group.add_command(scratch_migrate)


__all__ = ["human_bytes", "register", "scratch_migrate"]
