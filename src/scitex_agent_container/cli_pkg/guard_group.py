#!/usr/bin/env python3
# File: src/scitex_agent_container/cli_pkg/guard_group.py

"""``sac guard`` — mechanical gates over a delegated code change.

One verb today: ``sac guard deletions``. It exists so the
unrequested-deletion check is reachable from a hook, a shell, or any agent
— not only from the trials harness that first needed it. A gate with
exactly one caller is a script; a gate anything can call is a gate.

The two existing static guards in this package
(``_hosted_runner_guard``, ``_runner_pool_guard``) are ``python -m``
entry points and are deliberately NOT moved here in this change — they
are CI-workflow guards with their own wiring, and relocating a published
entry point is a migration, not a rename.
"""

from __future__ import annotations

import json as _json

import click

from ._helpers import HelpRecursiveGroup

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(
    "guard", cls=HelpRecursiveGroup, context_settings=CONTEXT_SETTINGS
)
def guard_group() -> None:
    """Mechanical gates a delegated change must pass."""


@guard_group.command("deletions", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--repo",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Repository to inspect (used with --base/--target).",
)
@click.option("--base", default=None,
              help="Git ref holding the BEFORE tree (e.g. HEAD, origin/develop).")
@click.option("--target", default=None,
              help="Git ref holding the AFTER tree. Default: the working tree.")
@click.option("--before", type=click.Path(), default=None,
              help="BEFORE snapshot directory (pairs with --after).")
@click.option("--after", type=click.Path(), default=None,
              help="AFTER snapshot directory (pairs with --before).")
@click.option(
    "--allow",
    "allowed",
    multiple=True,
    metavar="KEY",
    help="A deletion the task DID require: 'path::func:name', "
         "'path::class:Name', or a bare path for a whole file. Repeatable.",
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def deletions(repo, base, target, before, after, allowed, as_json) -> None:
    """Report code a change deleted WITHOUT being asked to.

    Compares the symbols (module-level defs and classes, plus methods) in
    a baseline tree against the same paths afterwards, and names anything
    that vanished and was not passed to ``--allow``.

    \b
    Three verdicts, three exit codes — a bare bool would collapse the
    third into one of the others, and that collapse is the whole bug:
      0  clean                nothing vanished unrequested
      3  violations           named deletions, listed with their lines
      4  could-not-determine  no baseline / not a git repo / unreadable
                              tree / a file that no longer parses

    Exit 4 is NOT a pass. 1 and 2 are left to their universal meanings
    (generic failure, usage error) so a renamed verb cannot impersonate
    a domain answer.

    \b
    A symbol is a def or a class, never an import — so replacing a
    ``def foo`` with ``from elsewhere import foo`` reports a deletion
    even though ``foo`` still imports. Clear that with --allow.

    \b
    Examples:
      $ sac guard deletions --base HEAD                 # worktree vs HEAD
      $ sac guard deletions --base origin/develop --target HEAD
      $ sac guard deletions --before /tmp/a --after /tmp/b
      $ sac guard deletions --base HEAD --allow 'calc.py::func:clamp'
      $ sac guard deletions --base HEAD --json
    """
    from .._guard import check_deletions, render

    report = check_deletions(
        repo=repo,
        base=base,
        target=target,
        before=before,
        after=after,
        allowed=allowed,
    )
    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        click.echo(render(report), err=report.exit_code != 0)
    raise SystemExit(report.exit_code)


__all__ = ["deletions", "guard_group"]

# EOF
