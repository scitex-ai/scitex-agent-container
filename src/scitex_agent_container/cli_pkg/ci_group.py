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
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def runners(ctx: click.Context, target: str, repo: str, as_json: bool) -> None:
    """Show which RUNNER executed each job, and fail on a banned one.

    A green check is not proof of compliance. scitex-dev PR #572 showed
    ``import-smoke pass 1m9s`` while the jobs API said
    ``runner_name=spartan-cpu-org-01`` — it had been queued before the repo
    was repointed off Spartan, so it ran on the very hardware the change
    existed to abandon. Re-reading the CI_RUNS_ON variable would also have
    said green. Only the job's actual runner disagreed.

    Exits non-zero when any job ran on a banned runner, so it works as a gate.

    \b
    Examples:
      $ sac ci runners 572                    # every run behind a PR
      $ sac ci runners 31546807064            # one run id
      $ sac ci runners develop --repo o/n     # a branch, explicit repo
    """
    try:
        runs = _runner_audit(target, repo=repo)
    except CIWhyError as exc:
        # UNKNOWN is not compliant: fail loud rather than print "all clear".
        raise click.ClickException(str(exc)) from exc

    if _json_flag(ctx, as_json):
        click.echo(_json.dumps([r.to_dict() for r in runs], indent=2))
    else:
        for r in runs:
            click.echo(f"run {r.run_id}")
            click.echo(_render_runners(r))

    violations = [j for r in runs for j in r.violations]
    if violations:
        names = ", ".join(sorted({j.runner_name or "?" for j in violations}))
        raise click.ClickException(
            f"{len(violations)} job(s) ran on BANNED runners: {names}"
        )


__all__ = ["ci_group"]
