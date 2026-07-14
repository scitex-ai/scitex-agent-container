"""``sac freshness`` — is the code we are running the code that shipped?

Two verbs, split by who can afford to wait:

* ``refresh`` — does the network I/O (PyPI, git tags, gh runs), writes the
  cache. Run from cron. Nobody is waiting on it.
* ``check``   — reads the cache and reports. No network, no waiting. This
  is also what the every-invocation CLI warning reads.

Exit codes are chosen so this is safe to put in a pipeline:

* ``0`` — FRESH, **or UNKNOWN**. Not knowing is not a failure. A check
  that exits non-zero when it cannot see turns every offline laptop and
  every slow CI runner into a red build, and gets removed.
* ``3`` — STALE. Positive evidence something did not ship, or is not
  running. The only actionable state.
"""

from __future__ import annotations

import json as _json

import click

from .._freshness._cache import cache_path, read_cache, write_cache
from .._freshness._model import Freshness
from .._freshness._warn import EXIT_STALE

_STYLE = {
    Freshness.FRESH: ("green", "FRESH"),
    Freshness.STALE: ("red", "STALE"),
    Freshness.UNKNOWN: ("yellow", "UNKNOWN"),
}


@click.group(
    "freshness",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def freshness_group() -> None:
    """Deploy freshness: ghost tags, stale installs, stale daemons.

    \b
    Examples:
      $ sac freshness check              # read the cache (no network)
      $ sac freshness refresh            # re-probe PyPI/git/gh, write cache
      $ sac freshness check --json
    """


def _render(report, *, verbose: bool) -> None:
    """Print a report. STALE findings always; the rest under --verbose.

    FRESH findings still carry information worth having on demand — the
    ghost-tag check names every superseded ghost even when it raises no
    alarm — so ``check`` prints them and only the every-invocation
    warning stays terse.
    """
    colour, label = _STYLE[report.state]
    click.secho(f"deploy freshness: {label}", fg=colour, bold=True)

    for finding in report.findings:
        f_colour, f_label = _STYLE[finding.state]
        if finding.state is Freshness.STALE or verbose:
            click.secho(f"  [{f_label:^7}] {finding.check}", fg=f_colour, nl=False)
            click.echo(f" — {finding.summary}")
            if finding.remedy:
                click.echo(f"            fix: {finding.remedy}")
            if verbose and finding.detail:
                click.echo(f"            {finding.detail}")

    if report.state is Freshness.UNKNOWN and not verbose:
        click.echo("  (UNKNOWN is not 'fine' — rerun with -v to see why)")


@freshness_group.command("check")
@click.option("--json", "as_json", is_flag=True, default=False, help="Structured output.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Show FRESH/UNKNOWN findings too.")
def freshness_check(as_json: bool, verbose: bool) -> None:
    """Report cached freshness. No network — reads what `refresh` wrote."""
    report = read_cache()
    if report is None:
        # UNKNOWN, and we say WHY. Silence is right for the every-command
        # warning; here the operator asked, so we owe an answer.
        msg = (
            f"deploy freshness: UNKNOWN — no usable cache at {cache_path()}\n"
            "  (missing, corrupt, or older than the TTL)\n"
            "  fix: sac freshness refresh"
        )
        if as_json:
            click.echo(_json.dumps({"state": "unknown", "reason": "no-cache", "findings": []}, indent=2))
        else:
            click.secho(msg, fg="yellow")
        raise SystemExit(0)

    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        _render(report, verbose=verbose)

    raise SystemExit(EXIT_STALE if report.state is Freshness.STALE else 0)


@freshness_group.command("refresh")
@click.option("--json", "as_json", is_flag=True, default=False, help="Structured output.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Show FRESH/UNKNOWN findings too.")
@click.option(
    "--unit",
    default="sac-listen.service",
    show_default=True,
    help="systemd unit whose running code is compared against the install.",
)
def freshness_refresh(as_json: bool, verbose: bool, unit: str) -> None:
    """Re-probe PyPI / git tags / gh runs / symbols, then write the cache.

    This is the only surface that touches the network. Run it from cron;
    everything else reads its output.
    """
    import sys

    from .._freshness._checks import build_report
    from .._freshness._sources import LiveSources

    report = build_report(LiveSources(), unit=unit, python=sys.executable)
    path = write_cache(report)

    if as_json:
        payload = report.to_dict()
        payload["cache"] = str(path)
        click.echo(_json.dumps(payload, indent=2))
    else:
        _render(report, verbose=verbose)
        click.echo(f"  cache: {path}")

    raise SystemExit(EXIT_STALE if report.state is Freshness.STALE else 0)


# EOF
