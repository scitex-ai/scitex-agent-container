#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/cli_pkg/freshness_cmds.py

"""``sac freshness`` — is the sac I am running the sac that shipped?

Three verbs, split by who can afford to wait:

* ``check``   — run every check now, against the network. For a human who
  is asking on purpose.
* ``refresh`` — same checks, but publish the result to the cache. The cron
  payload; it pays the network cost off the interactive path so the CLI
  never does.
* ``status``  — read the cache and print it. Cheap enough to run anywhere,
  and the same thing the startup banner reads.

The verdict logic is entirely upstream in ``scitex_dev.versioning``; this
module renders it. Rendering is not a small responsibility here — the four
properties the primitive guarantees can all be thrown away at the display
layer, so this module's job is to not do that:

1. UNKNOWN is never printed as green/OK. It gets its own word and its own
   exit code.
2. Only STALE findings carry a remedy, and the remedy printed is the one the
   primitive chose. This module never composes an upgrade command of its
   own — that is how an editable checkout gets handed a ``pip install -U``
   that clobbers it.
3. The origin + interpreter stamp is shown, not stripped. It is already
   folded into each finding's summary upstream, and it is reprinted as a
   header line so it is visible even when the summary is long.
"""

from __future__ import annotations

import json as _json

import click

# Exit codes. Distinct on purpose: a caller scripting against this must be
# able to tell "behind" from "could not tell", which is exactly the
# distinction a boolean would destroy.
EXIT_FRESH = 0
EXIT_STALE = 1
EXIT_UNKNOWN = 2


def _report_header(report) -> str:
    """The 'who is speaking' line: which install, which interpreter.

    Pulled from the first finding's stamped ``data``. Without it a banner
    reading "0.21.21 is behind 0.21.24" cannot be acted on, because the
    reader does not know which of several installs it describes.
    """
    for finding in report.findings:
        origin = finding.data.get("origin")
        executable = finding.data.get("executable")
        if origin or executable:
            return f"install: {origin or '?'}\npython : {executable or '?'}"
    return "install: ?\npython : ?"


def _render(report, as_json: bool) -> tuple[str, int]:
    """Render a report (or UNKNOWN) to ``(text, exit_code)``."""
    if report is None:
        if as_json:
            return (
                _json.dumps(
                    {
                        "state": "unknown",
                        "reason": "scitex-dev is not installed in this "
                        "interpreter, so no verdict could be obtained",
                        "findings": [],
                    },
                    indent=2,
                ),
                EXIT_UNKNOWN,
            )
        return (
            "version-currency: UNKNOWN — the checker itself is unavailable.\n"
            "  scitex_dev.versioning could not be imported in this "
            "interpreter.\n"
            "  This is NOT a clean bill of health; nothing was checked.\n"
            "  fix: pip install 'scitex-agent-container[dev]'",
            EXIT_UNKNOWN,
        )

    if as_json:
        payload = report.to_dict()
        return _json.dumps(payload, indent=2), _exit_for(report)

    lines = [_report_header(report), ""]
    for finding in report.findings:
        lines.append(f"[{finding.state.value.upper():<7}] {finding.check}")
        lines.append(f"    {finding.summary}")
        if finding.remedy:
            lines.append(f"    fix: {finding.remedy}")
    lines.append("")
    lines.append(f"verdict: {report.state.value.upper()}")
    return "\n".join(lines), _exit_for(report)


def _exit_for(report) -> int:
    """Map the tri-state verdict onto three distinct exit codes."""
    from scitex_dev.versioning import Currency

    if report.state is Currency.STALE:
        return EXIT_STALE
    if report.state is Currency.FRESH:
        return EXIT_FRESH
    return EXIT_UNKNOWN


@click.group("freshness")
def freshness() -> None:
    """Is the running sac the sac that shipped?

    \b
    Exit codes:
      0  FRESH   — positively current
      1  STALE   — positively behind (the only actionable state)
      2  UNKNOWN — could not tell. NOT the same as fresh.
    """


@click.command("check")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full report as JSON.",
)
def check(as_json: bool) -> None:
    """Run every currency check now (hits PyPI, git, gh, systemd)."""
    from .._freshness import check_currency

    text, code = _render(check_currency(), as_json)
    click.echo(text)
    raise SystemExit(code)


@click.command("refresh")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full report as JSON.",
)
def refresh(as_json: bool) -> None:
    """Run the checks and publish the result to the cache (the scheduled verb).

    \b
    Exit codes here answer "did I publish a report?", NOT "is sac current?":
      0  a report was written, whatever its verdict
      2  no report could be produced

    That is deliberately different from ``check``. This verb runs on a timer,
    and a timer that exits non-zero whenever the ANSWER is stale marks its
    unit failed for as long as the staleness lasts — which conflates "the
    refresher is broken" with "the refresher is working and has bad news".
    Those need different responses, so they get different exit codes. The
    verdict is still printed, and ``check`` still reports it tri-state.
    """
    from .._freshness import refresh_cache

    report = refresh_cache()
    text, _verdict_code = _render(report, as_json)
    click.echo(text)
    raise SystemExit(EXIT_FRESH if report is not None else EXIT_UNKNOWN)


@click.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the cached report as JSON.",
)
def status(as_json: bool) -> None:
    """Print the last cached verdict without touching the network.

    An expired cache reads as UNKNOWN, not as the last known answer: a dead
    refresher's final verdict is a fossil, not evidence about now.
    """
    from .._freshness import read_cached

    text, code = _render(read_cached(), as_json)
    click.echo(text)
    raise SystemExit(code)


freshness.add_command(check)
freshness.add_command(refresh)
freshness.add_command(status)


# EOF
