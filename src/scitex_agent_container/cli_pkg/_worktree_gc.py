"""``sac worktree gc`` — reap safe, stale worktrees; shout about the rest.

Registered onto the ``worktree`` group by :func:`register`, exactly as
:mod:`._host_sync` does onto ``host``.

Rendering rule for this whole file, inherited from the host-sync verb:
**there is no quiet success path.** A pass that removes nothing still
prints what it judged and why it kept each one, because a GC that reaps
what it can and stays silent about what it cannot is how a repo reaches
105 worktrees behind a green cron line. The kept-reasons breakdown is the
product; the removals are the easy half.
"""

from __future__ import annotations

import json

import click

from .._maintenance import (
    DEFAULT_CAP,
    DEFAULT_MIN_AGE_HOURS,
    GcOutcome,
    RepoGcResult,
    discover_repos,
    exit_code_for,
    gc_repos,
    record_gc_results,
)
from ._helpers import _json_flag, console


def _evidence(text: str) -> None:
    """Print one evidence line WITHOUT rich's word-wrap.

    A wrapped absolute path is a path you cannot grep out of a cron log,
    and every line here exists to be read back later.
    """
    console.print(text, soft_wrap=True)


def _print_repo(result: RepoGcResult) -> None:
    """Everything that happened to one repo. All of it gets printed."""
    if result.unreadable:
        _evidence(f"[magenta]UNKNOWN[/magenta]   {result.repo}")
        _evidence(f"    [red]could not read repo: {result.error}[/red]")
        console.print("")
        return

    colour = "yellow" if result.exceeds_cap else "green"
    label = "OVER CAP" if result.exceeds_cap else "ok"
    _evidence(
        f"[{colour}]{label:<9}[/{colour}] {result.repo}  "
        f"[dim]{result.count_after} worktree(s), cap {result.cap}[/dim]"
    )
    for verdict in result.removed:
        _evidence(f"    [green]removed[/green]    {verdict.path}")
    for verdict in result.kept:
        if verdict.removable:
            # Dry-run: proven safe, deliberately untouched.
            _evidence(f"    [cyan]would remove[/cyan] {verdict.path}")
            continue
        reasons = ", ".join(verdict.keep_reasons)
        _evidence(f"    [dim]kept[/dim]       {verdict.path}  [dim]({reasons})[/dim]")
        if verdict.remove_error:
            _evidence(f"      [red]git refused: {verdict.remove_error}[/red]")
    breakdown = result.keep_reason_breakdown
    if breakdown:
        summary = ", ".join(f"{n} {reason}" for reason, n in breakdown.items())
        _evidence(f"    [dim]kept-reasons: {summary}[/dim]")
    if result.prune_detail:
        for line in result.prune_detail.splitlines():
            _evidence(f"    [dim]prune: {line}[/dim]")
    console.print("")


def _print_report(outcome: GcOutcome, *, apply: bool) -> None:
    mode = "apply" if apply else "dry-run (read-only)"
    console.print(
        f"[bold]sac worktree gc[/bold]  {mode} — {len(outcome.results)} repo(s)\n"
    )
    for result in outcome.results:
        _print_repo(result)

    # Never silent: say what the verdict MEANS, not just what it was.
    if outcome.over_cap:
        names = ", ".join(r.repo for r in outcome.over_cap)
        console.print(
            f"[yellow]{len(outcome.over_cap)} repo(s) still over cap:[/yellow] {names}\n"
            "  Every kept worktree failed at least one safety leg (see the\n"
            "  kept-reasons above). The GC will NEVER auto-remove those — a\n"
            "  human decides. Dirty worktrees hold work that exists nowhere else."
        )
    elif outcome.unreadable:
        console.print(
            f"[magenta]{len(outcome.unreadable)} repo(s) UNREADABLE[/magenta] "
            "[dim]— unknown is not clean; their sprawl is unobserved, not absent.[/dim]"
        )
    else:
        console.print(
            "[green]every repo under its worktree cap[/green] "
            "[dim](removals are proven-safe only: clean AND merged AND aged "
            "AND idle)[/dim]"
        )
    if not apply:
        console.print(
            "[dim]dry-run: nothing was removed. Re-run with --apply to act.[/dim]"
        )


@click.command("gc")
@click.option(
    "--repo",
    "repos",
    multiple=True,
    type=click.Path(),
    help="Repo to sweep (repeatable). Mutually exclusive with --all.",
)
@click.option(
    "--all",
    "all_repos",
    is_flag=True,
    default=False,
    help=(
        "Every local git repo declared as an agent's spec.workdir "
        "(sac's own spec tree is the source — see `sac worktree gc --help`)."
    ),
)
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACT: remove the worktrees the predicate proved safe. Default is --dry-run.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="READ-ONLY report (the DEFAULT). Removes nothing; explicit for scripts.",
)
@click.option(
    "--min-age-hours",
    type=float,
    default=DEFAULT_MIN_AGE_HOURS,
    show_default=True,
    help="Keep any worktree whose HEAD commit is younger than this.",
)
@click.option(
    "--cap",
    type=int,
    default=DEFAULT_CAP,
    show_default=True,
    help="Alarm when a repo still has more than this many worktrees after the pass.",
)
@click.option(
    "--alarm/--no-alarm",
    "alarm",
    default=None,
    help=(
        "Record each repo's cap verdict in sac's own event log "
        "(upsert over cap / unknown, resolve back under). Default: on with "
        "--apply, off on a dry run (a report stays a pure report)."
    ),
)
@click.option(
    "--exit-zero",
    "exit_zero",
    is_flag=True,
    default=False,
    help=(
        "Always exit 0. The verdict is unchanged and still printed, still in "
        "the JSON `exit_code`, and still recorded by --alarm — only the "
        "PROCESS status is neutralised. For unattended runners where a "
        "non-zero status means 'this job is unhealthy', not 'this job found "
        "something a human must decide'."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.pass_context
def worktree_gc(
    ctx: click.Context,
    repos: tuple[str, ...],
    all_repos: bool,
    apply: bool,
    dry_run: bool,
    min_age_hours: float,
    cap: int,
    alarm: bool | None,
    exit_zero: bool,
    as_json: bool,
) -> None:
    """Remove worktrees that are PROVABLY safe to remove. Keep everything else.

    Agent-tool worktrees auto-clean only when nothing edited them; anything
    an agent touched persists forever. One repo reached 105 worktrees and
    helped trigger a host load-spike. This is the permanent countermeasure.

    \b
    Report (read-only — the DEFAULT):
      $ sac worktree gc --repo ~/proj/scitex-todo
      $ sac worktree gc --all --json
    \b
    Act:
      $ sac worktree gc --apply --all

    A worktree is removed ONLY if ALL FOUR legs pass:

    \b
      CLEAN     `git status --porcelain` is empty. Untracked counts as
                DIRTY — it is work saved nowhere else.
      MERGED    an ancestor of develop/main, OR it has a MERGED PR (the
                squash-merge case, which the ancestor check alone calls
                unmerged forever).
      OLD       its HEAD commit is older than --min-age-hours.
      IDLE      no running process has its cwd inside it (best-effort
                /proc scan; if the signal is unavailable -> KEEP).

    Every leg is three-state: a check that could not RUN keeps the
    worktree, exactly like a check that failed. Removal is `git worktree
    remove` with NO --force, so git's own dirty-refusal is a second,
    independent backstop. `git worktree prune` (admin refs whose directory
    is already gone — destroys no files) runs alongside, as --dry-run on a
    dry run.

    \b
    --all's repo source is sac's OWN spec tree: every agent spec.yaml's
    `spec.workdir` that (a) exists on THIS host and (b) is a git repo
    toplevel. A repo no agent declares is never swept by --all — name it
    with --repo.

    \b
    Exit codes:  0 = all repos under cap.  1 = a repo is still over cap
    after the pass.  2 = a repo could not be READ (unknown outranks
    known-bad: it is a known-bad you cannot see).  --exit-zero keeps the
    verdict in the report and the JSON but always exits 0.
    """
    if bool(repos) == all_repos:
        raise click.UsageError(
            "give exactly one of --repo PATH (repeatable) or --all  "
            "(e.g. `sac worktree gc --repo ~/proj/scitex-todo` or "
            "`sac worktree gc --apply --all`)"
        )
    if apply and dry_run:
        raise click.UsageError(
            "--apply and --dry-run are opposites; --dry-run is already the default"
        )

    targets = list(discover_repos()) if all_repos else list(repos)
    if not targets:
        raise click.UsageError(
            "no local git repo is declared as any agent's spec.workdir, so "
            "--all has nothing to sweep. Name one explicitly:  "
            "sac worktree gc --repo <path>"
        )

    outcome = gc_repos(
        targets,
        apply=apply,
        min_age_hours=min_age_hours,
        cap=cap,
    )
    code = exit_code_for(outcome)
    # MEASURED 2026-08-20: this verb runs as
    # scitex-agent-container-worktree-gc under the ecosystem supervisor. It
    # exited 1 on all three of its runs, which reads as a broken job — and it
    # was not broken. Exit 1 here means "a repo is still over cap", i.e. every
    # kept worktree failed a safety leg and a HUMAN must decide. That is a
    # finding, not ill health.
    #
    # The cost was concrete: a duplicate systemd timer for this same job could
    # not be retired, because the rule for retiring one was "the supervisor has
    # a recorded exit-0 for it" and this job could never produce one. A finding
    # rendered as a failure kept a second scheduler alive.
    #
    # Its sibling `sac host sync --check` carries the same flag for the same
    # reason, and its comment records where that confusion led last time: a
    # unit marked `failed` for doing its job put the host into `degraded`, and
    # an installer read `degraded` as "systemd is absent" and silently stopped
    # installing dotfiles sync there.
    #
    # THE FINDING IS NOT DISCARDED, and that is the condition for using this
    # flag at all. A flag that always exits 0 does destroy the distinction if
    # the exit code is the only place the distinction lives. Here it is not:
    # `--alarm` (on by default with --apply, which is how the scheduled job
    # runs) records every repo's cap verdict in sac's own event log, and the
    # verdict is still printed and still in the JSON `exit_code`. Neutralising
    # the PROCESS status moves the signal onto the rail built for it instead of
    # the one systemd reads as health.
    #
    # A job with no such rail must NOT reach for this flag — for those, the
    # runner needs to carry "finished with findings" as its own outcome rather
    # than folding it into ok/not-ok. That belongs in the JobSpec contract, and
    # scitex-dev owns it.
    status = 0 if exit_zero else code

    # Make the shout DURABLE: record each cap verdict in sac's own event
    # log. Defaults to riding --apply only, so a dry run stays a pure report.
    do_alarm = apply if alarm is None else alarm
    alarm_outcome = record_gc_results(list(outcome.results)) if do_alarm else None

    if _json_flag(ctx, as_json):
        payload: dict = {
            "mode": "apply" if apply else "dry-run",
            "exit_code": code,
            "cap": cap,
            "min_age_hours": min_age_hours,
            "removed": outcome.removed_count,
            "kept": outcome.kept_count,
            "repos": [r.to_dict() for r in outcome.results],
        }
        if alarm_outcome is not None:
            payload["alarm"] = {
                "degraded": list(alarm_outcome.degraded),
                "unknown": list(alarm_outcome.unknown),
                "recovered": list(alarm_outcome.recovered),
                "failed": list(alarm_outcome.failed),
            }
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(status)

    _print_report(outcome, apply=apply)
    console.print(f"[dim]{outcome.summary_line()}[/dim]")
    if alarm_outcome is not None:
        console.print(f"[dim]{alarm_outcome.summary_line()}[/dim]")
    raise SystemExit(status)


def register(worktree_group) -> None:
    """Attach ``gc`` to the parent ``worktree`` Click group."""
    worktree_group.add_command(worktree_gc)


__all__ = ["register", "worktree_gc"]
