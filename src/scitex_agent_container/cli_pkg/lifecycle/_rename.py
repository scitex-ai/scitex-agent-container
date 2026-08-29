#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents rename <old> <new>`` — rename an agent everywhere, atomically.

An agent's name lives in six places on disk plus the shared task board.
Renaming by hand means editing all of them consistently, and the one you
miss is silent — most damagingly the board identity
(``SCITEX_TODO_AGENT_ID``): change it without migrating the cards and
every card the agent owns is orphaned, with nothing to tell you.

The engine is :mod:`..._lifecycle._rename`; this module is its CLI face —
argument parsing, the ``--dry-run`` report, and the confirmation gate.

Output goes through ``click.echo``, not the shared rich ``console``: the
report is full of literal ``[spec-dir]``-style labels and bracketed
``raw_args[3]`` paths, and rich would parse every one of those as a markup
tag and blow up on the unknown style. Colour comes from ``click.style``,
which leaves square brackets alone.
"""

from __future__ import annotations

import json as _json

import click

from ..._lifecycle._rename import STEPS, agent_rename
from ..._lifecycle._rename_plan import Layout, RenameError, RenamePlan, build_plan
from ..._lifecycle._rename_spec import SpecChange
from .._helpers import agent_name_complete

__all__ = ["rename"]

# The env var whose value IS the agent's identity on the board. Called out
# explicitly in the report because it is the one whose silent breakage
# costs real work.
# Both spellings are live on disk during the rename, so the marker looks for
# BOTH. Checking only the retired one silently drops the highlight on the
# majority of specs -- measured 2026-08-25: 110 specs on the current name
# vs 21 on the retired one.
_BOARD_IDENTITY_ENVS = ("SCITEX_CARDS_AGENT_ID", "SCITEX_TODO_AGENT_ID")

_MAX_LISTED_CARDS = 8


def _mark(change: SpecChange) -> str:
    """Annotate the spec change that carries the board identity."""
    if not any(env in change.path for env in _BOARD_IDENTITY_ENVS):
        return ""
    return "   " + click.style("<-- BOARD IDENTITY", fg="yellow", bold=True)


def _render_plan(plan: RenamePlan, *, dry_run: bool) -> None:
    """Print the full plan. Identical for --dry-run and the real run."""
    if dry_run:
        click.echo(click.style("DRY RUN — nothing will be changed.", bold=True))
        click.echo("")
    click.echo(
        f"rename  {click.style(plan.old, bold=True)}"
        f"  ->  {click.style(plan.new, bold=True)}\n"
    )

    click.echo("  [spec-dir]")
    click.echo(f"      {plan.spec_move.src}")
    click.echo(f"   -> {plan.spec_move.dst}\n")

    click.echo(f"  [spec-file]    {len(plan.spec_changes)} self-reference(s)")
    for change in plan.spec_changes:
        click.echo(f"      {change.render()}{_mark(change)}")
    click.echo("")

    for label, move in (
        ("overlay-dir", plan.overlay_move),
        ("runtime-dir", plan.runtime_move),
        ("registry", plan.registry_move),
    ):
        if move is None:
            click.echo(f"  [{label}]     (none — skipped)")
            continue
        click.echo(f"  [{label}]")
        click.echo(f"      {move.src}")
        click.echo(f"   -> {move.dst}")
    click.echo("")

    click.echo(
        f"  [state-db]     {plan.db_total} row(s) across "
        f"{len(plan.db_counts)} column(s)"
    )
    for column, count in sorted(plan.db_counts.items()):
        click.echo(f"      {column:<34} {count}")
    click.echo("")

    _render_cards(plan)
    _render_warnings(plan)


def _render_cards(plan: RenamePlan) -> None:
    if not plan.cards_enabled:
        click.echo("  [cards]        SKIPPED (--no-cards)\n")
        return
    n = len(plan.card_ids)
    click.echo(f"  [cards]        {n} card(s) -> owner '{plan.new}'")
    shown = plan.card_ids[:_MAX_LISTED_CARDS]
    if shown:
        click.echo(f"      {', '.join(shown)}")
        if n > len(shown):
            click.echo(f"      ... (+{n - len(shown)} more)")
    click.echo(
        "      via scitex_todo._store.reassign_task — sac calls the board's own\n"
        "      primitive; it never edits the store itself.\n"
    )


def _render_warnings(plan: RenamePlan) -> None:
    if not plan.warnings:
        return
    click.echo(click.style("WARNINGS", fg="yellow", bold=True))
    for warning in plan.warnings:
        click.echo(f"  {click.style('!', fg='yellow')} {warning}")
    click.echo("")


def _stakes(plan: RenamePlan) -> str:
    """Name what the rename would move, for the refuse-without-yes line."""
    if plan.cards_enabled and plan.card_ids:
        return f" and reassign {len(plan.card_ids)} card(s)"
    return ""


def _plan_json(plan: RenamePlan, *, dry_run: bool, applied: bool) -> str:
    return _json.dumps(
        {
            "old": plan.old,
            "new": plan.new,
            "dry_run": dry_run,
            "applied": applied,
            "spec_dir": {
                "src": str(plan.spec_move.src),
                "dst": str(plan.spec_move.dst),
            },
            "spec_changes": [
                {"path": c.path, "before": c.before, "after": c.after}
                for c in plan.spec_changes
            ],
            "moves": {
                label: (
                    None
                    if move is None
                    else {"src": str(move.src), "dst": str(move.dst)}
                )
                for label, move in (
                    ("overlay", plan.overlay_move),
                    ("runtime", plan.runtime_move),
                    ("registry", plan.registry_move),
                )
            },
            "state_db_rows": plan.db_counts,
            "cards": {
                "enabled": plan.cards_enabled,
                "ids": plan.card_ids,
                "count": len(plan.card_ids),
            },
            "warnings": plan.warnings,
            "steps": list(STEPS),
        },
        ensure_ascii=False,
    )


@click.command(name="rename")
@click.argument("old", type=str, shell_complete=agent_name_complete)
@click.argument("new", type=str)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print every location the rename would touch. Changes nothing.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help=(
        "Required to apply. Without it the rename is REFUSED (exit 2) — it "
        "never prompts, so it cannot hang under cron, CI, or an agent shell."
    ),
)
@click.option(
    "--no-cards",
    "no_cards",
    is_flag=True,
    default=False,
    help=(
        "Do NOT migrate the agent's task cards. Every card it owns is then "
        "ORPHANED (the agent under its new name cannot see its own work). "
        "Only for a fleet with no board."
    ),
)
@click.option(
    "--store",
    "store",
    type=str,
    default=None,
    help="Explicit scitex-todo store path. Default: the resolved shared store.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the plan as a JSON envelope on stdout.",
)
def rename(
    old: str,
    new: str,
    dry_run: bool,
    yes: bool,
    no_cards: bool,
    store: str | None,
    as_json: bool,
) -> None:
    """Rename an agent everywhere — spec, dirs, state.db, and its task cards.

    An agent names itself in six places on disk plus the shared board.
    This verb changes all of them together, or none of them: every step
    records its inverse, and any failure rolls the whole rename back.

    \b
    What it touches:
      1. spec dir          ~/.scitex/agent-container/agents/<name>/
      2. spec self-refs    labels.project / labels.purpose / workdir /
                           overlay path / state-db path / board identity
      3. overlay dir       .../containers/overlays/<name>/
      4. runtime+state dir .../runtime/<name>/   (bound at /state/<name>)
      5. registry entry    .../runtime/registry/<name>.json
      6. state.db rows     every table that keys on the agent name
      7. TASK CARDS        reassigned via scitex-todo's own reassign_task

    \b
    Step 7 is the point of the verb. The board knows the agent by
    SCITEX_TODO_AGENT_ID; rename without migrating and every card the
    agent owns is orphaned — silently.

    \b
    The agent must be STOPPED (renaming a live agent's workdir and overlay
    out from under it is unsafe). Run --dry-run first; it is exact.

    \b
    Example:
      $ sac agents rename scitex-todo scitex-cards --dry-run
      $ sac agents rename scitex-todo scitex-cards -y
    """
    try:
        plan = build_plan(
            old,
            new,
            layout=Layout.default(),
            store=store,
            cards=not no_cards,
        )
    except RenameError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json and dry_run:
        click.echo(_plan_json(plan, dry_run=True, applied=False))
        return

    if not as_json:
        _render_plan(plan, dry_run=dry_run)

    if dry_run:
        click.echo(
            f"Re-run with -y to apply ({old!r} is stopped, so it is allowed)."
        )
        return

    if not yes:
        # REFUSE, never prompt. The ecosystem CLI convention (§2) is
        # `--yes`/`-y` plus refuse-without-yes, and it is the right rule: an
        # interactive confirm hangs forever under cron, CI, or an agent's
        # non-tty shell — on a verb that has already been asked to move a
        # live agent's dirs. Same shape as `sac agents delete`.
        click.echo(
            f"Refusing to rename {old!r} -> {new!r}{_stakes(plan)} "
            "without --yes/-y.\n"
            "Run --dry-run first — it prints every location this touches.",
            err=True,
        )
        raise SystemExit(2)

    def _progress(step: str) -> None:
        if not as_json:
            click.echo(f"  .. {step}")

    try:
        # Re-plans (and so re-preflights) immediately before applying, which
        # closes the window between the report above and the mutation below:
        # an agent that STARTED in between is caught here, not half-renamed.
        applied = agent_rename(
            old,
            new,
            layout=plan.layout,
            store=store,
            cards=not no_cards,
            on_step=_progress,
        )
    except RenameError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(_plan_json(applied, dry_run=False, applied=True))
        return

    click.echo(f"\n{click.style('renamed', fg='green', bold=True)} {old} -> {new}")
    if applied.cards_enabled and applied.card_ids:
        click.echo(f"  {len(applied.card_ids)} card(s) now owned by {new!r}")
    click.echo(f"  Start it with:  sac agents start {new}")
