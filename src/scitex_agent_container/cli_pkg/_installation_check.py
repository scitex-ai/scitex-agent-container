"""``sac installation check`` — is this venv's install layout still true?

WHY HERE, and not somewhere else. Three homes were candidates:

* ``sac doctor`` is scoped, by its own docstring and its ``--fleet`` /
  ``--strict`` contract, to ONE subject: agent-spec source git drift.
  Folding a venv-layout audit under the same verb would make one exit
  code mean two unrelated things.
* ``sac provenance`` is the closest in CONCERN ("which code is really
  loaded") but is SELF-scoped by construction: it audits only
  ``scitex-agent-container``'s own dist, on the running ``sys.path``, and
  its whole product is the identity of the loaded code. This check is
  VENV-scoped and ALL-distributions, and can inspect a venv the current
  interpreter is not running from.
* ``sac installation`` is the INSTALL noun — already in the "Build &
  Install" help category, already the home of ``boot`` and ``setup-cron``.
  Those are both mutators; a read-only ``check`` is the completion of the
  noun rather than a new one, because "is this installation coherent?" is
  a property OF an installation.

Rendering rule, inherited from ``sac worktree gc``: **there is no quiet
success path, and UNKNOWN is never quiet either.** A distribution nobody
could read is printed as loudly as a broken one, because the entire
failure class this guard exists for spent days looking green.
"""

from __future__ import annotations

import json

import click

from .._maintenance import (
    IMPORTS_LIVE,
    DistributionVerdict,
    InstallIntegrityReport,
    inspect_install,
    install_integrity_exit_code,
)
from ._helpers import _json_flag, console

_STATE_STYLE = {"ok": "green", "broken": "red", "unknown": "magenta"}


def _evidence(text: str) -> None:
    """One evidence line WITHOUT rich's word-wrap.

    A wrapped absolute path is a path you cannot grep out of a cron log,
    and every line here exists to be read back later.
    """
    console.print(text, soft_wrap=True)


def _print_verdict(verdict: DistributionVerdict) -> None:
    style = _STATE_STYLE.get(verdict.state, "white")
    label = verdict.state.upper()
    tokens = ", ".join(verdict.reasons + verdict.unknown_reasons)
    _evidence(
        f"[{style}]{label:<8}[/{style}] {verdict.name}"
        + (f"  [dim]({tokens})[/dim]" if tokens else "")
    )
    for detail in verdict.details:
        _evidence(f"    [dim]{detail}[/dim]")


def _print_report(report: InstallIntegrityReport, *, show_all: bool) -> None:
    console.print(f"[bold]sac installation check[/bold]  {report.site_packages}\n")
    if report.note:
        _evidence(f"[yellow]note:[/yellow] {report.note}\n")

    if report.site_unknown:
        _evidence(f"[magenta]UNKNOWN[/magenta]  {report.site_detail}")
        return

    shown = report.verdicts if show_all else (report.broken + report.unknown)
    for verdict in shown:
        _print_verdict(verdict)
    if shown:
        console.print("")

    if report.import_resolution != IMPORTS_LIVE:
        _evidence(
            "[magenta]import resolution UNOBSERVABLE[/magenta] [dim]— a venv other "
            "than this interpreter's was inspected, so 'where does an import really "
            "land' was NOT checked. The path-level findings above are complete; "
            "that one leg is absent, not clean.[/dim]"
        )

    breakdown = report.reason_breakdown()
    if breakdown:
        summary = ", ".join(f"{n} {reason}" for reason, n in breakdown.items())
        _evidence(f"[red]BROKEN by reason:[/red] {summary}")
    if report.unknown:
        _evidence(
            f"[magenta]{len(report.unknown)} distribution(s) UNKNOWN[/magenta] "
            "[dim]— unknown is not clean; it is evidence nobody has.[/dim]"
        )
    if not report.broken and not report.unknown:
        _evidence(
            "[green]every distribution's install layout is coherent[/green] "
            "[dim](no dead/shadowed pointer, no orphaned or duplicated "
            "dist-info)[/dim]"
        )
    if not show_all and report.ok:
        _evidence(f"[dim]{len(report.ok)} ok row(s) hidden — pass --all to list.[/dim]")


@click.command("check")
@click.argument("venv", required=False, type=click.Path())
@click.option(
    "--dist",
    "dists",
    multiple=True,
    help="Only inspect these distributions (repeatable). Absent ones read UNKNOWN.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="List OK rows too (default prints only BROKEN and UNKNOWN).",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Also exit non-zero (2) on UNKNOWN — for a gate that needs a COMPLETE answer.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def installation_check(
    ctx: click.Context,
    venv: str | None,
    dists: tuple[str, ...],
    show_all: bool,
    strict: bool,
    as_json: bool,
) -> None:
    """Audit a venv's install layout: dead/shadowed pointers, fossil dist-info.

    READ-ONLY. Answers the question ``--version`` provably cannot: is the
    code this venv would run the code anybody thinks is installed?

    \b
    Two incidents, one class — the code executed was not the code believed
    installed, and BOTH reported a healthy version string the whole time:
      2026-07-16  an orphaned scitex_dev dist-info with no code behind it,
                  plus a .pth redirecting imports into an abandoned PR
                  worktree. Commands ran from a temp dir for days.
      2026-08-09  /opt/venv-sac's editable pointer targets a deleted
                  worktree on a deleted branch, while a real package dir
                  next to it shadows the pointer. Imports succeed;
                  propagation has been dead the whole time.

    \b
    Detects, per distribution:
      resolves-outside-site-packages  imports land outside site-packages and
                                      no LIVE editable pointer explains it
      dead-pointer                    an editable pointer whose target is gone
      shadowed-pointer                pointer AND a real package dir coexist —
                                      the copy wins, the pointer is inert
      orphaned-dist-info              dist-info with no code behind it
      duplicate-dist-info             two dist-info dirs for one distribution

    \b
    Every distribution is THREE-VALUED. UNKNOWN (unreadable site-packages,
    an absent distribution, evidence too thin to decide) is never folded
    into either pole: it prints as loudly as a break but does NOT fail the
    exit code, because "I could not look" is not "it is broken". Pass
    --strict when you need a complete answer rather than no bad news.

    \b
    Examples:
      $ sac installation check                      # this interpreter's venv
      $ sac installation check /opt/venv-sac        # an explicit venv
      $ sac installation check --dist scitex-dev --all
      $ sac installation check --json

    \b
    Exit codes:  0 = nothing broken.  1 = at least one distribution BROKEN.
    2 = --strict and something is UNKNOWN.
    """
    report = inspect_install(venv, dists=dists)
    code = install_integrity_exit_code(report, strict=strict)

    if _json_flag(ctx, as_json):
        payload = report.to_dict()
        payload["exit_code"] = code
        payload["strict"] = strict
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)

    _print_report(report, show_all=show_all)
    console.print(f"[dim]{report.summary_line()}[/dim]")
    if report.broken:
        console.print(
            "[dim]This command REPAIRS NOTHING on purpose — it is the guard the "
            "repair is gated behind.[/dim]"
        )
    raise SystemExit(code)


def register(install_group) -> None:
    """Attach ``check`` to the parent ``installation`` Click group."""
    install_group.add_command(installation_check)


__all__ = ["installation_check", "register"]
