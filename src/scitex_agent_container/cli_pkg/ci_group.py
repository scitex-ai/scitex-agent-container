"""``sac ci`` — read a CI failure as cheaply as reading its status.

``sac ci why <pr|run|branch>`` fetches the failing run's log ONCE and
prints only the essential — failing test IDs + assertion lines, or the
``##[error]`` annotation for a setup failure — a few hundred bytes, not
the whole log. The real reason is now as cheap to read as the status
word, so there is no economic reason to skip it. See ``_ci_why`` for the
parser and the rationale.
"""

from __future__ import annotations

import json as _json

import click

from ._ci_runners import audit as _runner_audit
from ._ci_runners import render_text as _render_runners
from ._ci_why import CIWhyError, explain, render_text
from ._helpers import _json_flag


@click.group("ci", context_settings={"help_option_names": ["-h", "--help"]})
def ci_group() -> None:
    """Read WHY CI is red as cheaply as reading THAT it's red."""


def _run_header(run) -> str:
    bits = [f"run {run.run_id}"]
    if run.workflow:
        bits.append(run.workflow)
    if run.branch:
        bits.append(f"@ {run.branch}")
    head = "  ".join(bits)
    return f"{head}\n{run.url}" if run.url else head


@ci_group.command("why")
@click.argument("target", required=False, default="")
@click.option(
    "--repo",
    default=None,
    help="owner/name; defaults to the repo gh detects from the cwd.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def why(ctx: click.Context, target: str, repo: str, as_json: bool) -> None:
    """Extract the real failure(s) from a red CI run — compactly.

    \b
    TARGET may be:
      * a PR number   -> the run(s) behind its failing checks
      * a run id       -> that run
      * a branch name  -> the latest run for that branch
      * omitted        -> the latest run for the current branch

    \b
    Examples:
      $ sac ci why 712              # a PR
      $ sac ci why 29446283736      # a run id
      $ sac ci why fix/foo          # a branch
      $ sac ci why                  # current branch's latest run
      $ sac ci why 712 --json       # machine-readable
    """
    try:
        runs = explain(target, repo=repo)
    except CIWhyError as exc:
        # UNKNOWN is not green: fail loud rather than print "no failures".
        raise click.ClickException(str(exc)) from exc

    if _json_flag(ctx, as_json):
        click.echo(_json.dumps([r.to_dict() for r in runs], indent=2))
        return

    blocks = [f"{_run_header(r)}\n{render_text(r)}" for r in runs if r.failures]
    if not blocks:
        click.echo("no failures")
        return
    click.echo("\n\n".join(blocks))


@ci_group.command("runners")
@click.argument("target", required=False, default="")
@click.option(
    "--repo",
    default=None,
    help="owner/name; defaults to the repo gh detects from the cwd.",
)
@click.option(
    "--deny",
    multiple=True,
    metavar="SUBSTR",
    help="Fail if any job ran on a runner whose name contains SUBSTR. Repeatable.",
)
@click.option(
    "--expect",
    multiple=True,
    metavar="SUBSTR",
    help="Fail if any job ran on a runner NOT matching SUBSTR. Repeatable.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def runners(
    ctx: click.Context,
    target: str,
    repo: str,
    deny: tuple[str, ...],
    expect: tuple[str, ...],
    as_json: bool,
) -> None:
    """Show which RUNNER actually executed each job.

    A green check does not say where the work ran. scitex-dev PR #572 showed
    ``import-smoke pass 1m9s`` while the jobs API said
    ``runner_name=spartan-cpu-org-01`` — it had been queued before the repo's
    ``CI_RUNS_ON`` was repointed, so it ran somewhere nobody expected.
    Re-reading the variable would also have said green. Only the job's actual
    runner disagreed.

    Reports by default. Any GATE is yours to state, because a host policy can
    be repealed overnight and a tool that hardcodes one becomes a known-false
    gate. Prefer ``--expect``: it asserts where work SHOULD have run, so it
    still catches a host nobody thought to forbid.

    \b
    Examples:
      $ sac ci runners 572                          # every run behind a PR
      $ sac ci runners 31546807064                  # one run id
      $ sac ci runners develop --repo o/n           # a branch, explicit repo
      $ sac ci runners 572 --expect org-cpu         # assert where it ran
      $ sac ci runners 572 --deny spartan           # assert where it did not
    """
    try:
        runs = _runner_audit(target, repo=repo)
    except CIWhyError as exc:
        # UNKNOWN is not a clean bill of health: fail loud, never "all clear".
        raise click.ClickException(str(exc)) from exc

    if _json_flag(ctx, as_json):
        click.echo(_json.dumps([r.to_dict() for r in runs], indent=2))
    else:
        for r in runs:
            click.echo(f"run {r.run_id}")
            click.echo(_render_runners(r, deny=deny, expect=expect))

    denied = [j for r in runs for j in r.denied(deny)]
    unexpected = [j for r in runs for j in r.unexpected(expect)]
    problems = []
    if denied:
        names = ", ".join(sorted({j.where for j in denied}))
        problems.append(f"{len(denied)} job(s) ran on DENIED runners: {names}")
    if unexpected:
        names = ", ".join(sorted({j.where for j in unexpected}))
        problems.append(
            f"{len(unexpected)} job(s) ran somewhere unexpected: {names} "
            f"(expected a name containing: {', '.join(expect)})"
        )
    if problems:
        raise click.ClickException("; ".join(problems))


__all__ = ["ci_group"]
