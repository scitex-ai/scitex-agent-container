"""``sac pr sync-cards`` — one board card per open PR.

The operator's window onto :mod:`.._prlifecycle._sweep`. Rendering rule
(inherited from ``sac agents reconcile``): there is no quiet path. Every repo
this pass touched gets a line saying what we concluded and WHY — a silent skip
is indistinguishable from a bug, and an UNKNOWN that renders like a clean run
is the incident this whole feature exists to prevent.
"""

from __future__ import annotations

import json

import click

from .._prlifecycle import EXIT_UNKNOWN, resolve_repos, sync_cards
from ._helpers import _json_flag, console


@click.command(name="sync-cards")
@click.option(
    "--repo",
    "repos",
    multiple=True,
    help="owner/name to sweep (repeatable). Default: $SAC_PR_REPOS, else sac's own repo.",
)
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY write the cards. Without this, nothing is written.",
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="Report only, write nothing (dry-run). This is already the DEFAULT.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.pass_context
def sync_cards_cmd(
    ctx: click.Context,
    repos,
    apply: bool,
    check: bool,
    as_json: bool,
) -> None:
    """Upsert ONE board card per open PR; complete it when the PR is gone.

    A PR with no card is invisible to the fleet — which is how 35 open PRs
    accumulated unnoticed until 31 of them were force-closed by hand on
    2026-07-18. This verb is the fix: every open PR becomes a card carrying its
    title, author, age, draft state and CI status.

    \b
    It deliberately does NOT nudge. scitex-todo's stale-active sweep already
    nudges the owner of any open card left untouched; a second nudger would
    only race the first. sac supplies the FACT, scitex-todo supplies the
    reminder.

    \b
    Report (read-only — the DEFAULT):
      $ sac pr sync-cards --check
    \b
    Write the cards:
      $ sac pr sync-cards --apply

    \b
    Exits 0 clean, 1 if cards need writing or a write failed, 2 if the open-PR
    list COULD NOT BE READ. 2 is never rendered as 0: an unreadable backlog and
    an empty one are different facts, and confusing them shows a 35-PR pile-up
    as a clean board.
    """
    if apply and check:
        raise click.UsageError(
            "--apply and --check are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to write the cards."
        )

    resolved = resolve_repos(repos)
    outcome = sync_cards(resolved, apply=apply)
    code = outcome.exit_code()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "apply" if apply else "check",
                    "exit_code": code,
                    "unknown": bool(outcome.unreadable) or not outcome.sweeps,
                    "repos": list(resolved),
                    "counts": outcome.counts(),
                    "heartbeat_carded": outcome.heartbeat_ok,
                    "unknown_detail": list(outcome.unknown_detail),
                    "writes": [
                        {
                            "repo": sweep.repo,
                            "number": w.number,
                            "card_id": w.card_id,
                            "action": w.action,
                            "detail": w.detail,
                        }
                        for sweep in outcome.sweeps
                        for w in sweep.writes
                    ],
                },
                indent=2,
            )
        )
        raise SystemExit(code)

    mode = "apply" if apply else "check (read-only)"
    console.print(f"[bold]sac pr sync-cards[/bold]  {mode} — {len(resolved)} repo(s)\n")

    if not resolved:
        console.print(
            "[magenta]UNKNOWN[/magenta]        no repo resolved to sweep\n"
            "    [dim]Swept ZERO repos. That is a configuration fact, not a "
            "clean board — this pass determined NOTHING.[/dim]\n"
            "  Name them explicitly:\n"
            "    sac pr sync-cards --repo owner/name\n"
            "    export SAC_PR_REPOS=owner/name,owner/other"
        )
        raise SystemExit(code)

    for sweep in outcome.sweeps:
        if not sweep.readable:
            console.print(
                f"[magenta]UNKNOWN[/magenta]        {sweep.repo} "
                f"[{sweep.fetch.state.value}]",
                soft_wrap=True,
            )
            console.print(f"    [dim]{sweep.fetch.detail}[/dim]", soft_wrap=True)
            console.print(
                "    [dim]NO card was created, updated or completed for this "
                "repo. An unreadable PR list is NOT an empty one.[/dim]",
                soft_wrap=True,
            )
            continue
        console.print(
            f"[green]READ[/green]           {sweep.repo} — "
            f"{len(sweep.fetch.prs)} open PR(s)",
            soft_wrap=True,
        )
        for write in sweep.writes:
            colour = {
                "upserted": "green",
                "completed": "cyan",
                "would-write": "yellow",
                "failed": "red",
            }.get(write.action, "white")
            console.print(
                f"  [{colour}]{write.action:<12}[/{colour}] #{write.number} "
                f"[dim]{write.detail}[/dim]",
                soft_wrap=True,
            )

    console.print(f"\n[bold]{outcome.summary()}[/bold]")

    if code == EXIT_UNKNOWN:
        console.print(
            "\n[magenta]COULD NOT DETERMINE the state of the PR backlog.[/magenta]\n"
            "  This pass is exiting 2, NOT 0 — reporting success while blind is "
            "how a 35-PR backlog renders as a clean board.\n"
            "  Check the client:\n"
            "    gh auth status\n"
            "    gh pr list -R <owner/name> --state open"
        )
    raise SystemExit(code)


def register(group) -> None:
    """Attach ``sync-cards`` to the parent ``pr`` Click group."""
    group.add_command(sync_cards_cmd)


__all__ = ["register", "sync_cards_cmd"]
