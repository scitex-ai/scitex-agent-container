"""``sac agents env set`` / ``sac agents env unset`` — edit one agent's env.

Registered onto ``sac agents`` by :func:`register`, the same way
:mod:`._agents_reconcile` and :mod:`._agents_state` attach theirs. ``env`` is a
noun sub-group with two verbs rather than two hyphenated leaves, matching the
``sac dev {service,timer,cron} <verb>`` grammar already in the CLI: the pair
shares a target, a spec resolver and the ``--restart`` rule, and splitting them
into ``env-set``/``env-unset`` would spell that relationship out in a prefix
instead of in the tree.

WHY IT EXISTS
    Nothing in sac could set a spec's environment variable. The gap was filled
    on the host by ``~/.local/bin/sac-agent-env.sh``, a bash script that edited
    the YAML with ``sed`` and is retired by this command — see
    ``docs/hand-written-script-retirement-20260902.md``. Two of its defects are
    worth naming because they are what a verb has to beat, not merely match:

    * it compared the requested value against the FIRST ``^\\s+K:`` line in the
      whole document, so a key that also names a field in another block (``model:``
      under ``claude:``) was compared against the wrong field; and
    * its ``sed`` then rewrote EVERY indented ``K:`` line in the file.

    :mod:`...config._env_block_line` anchors on the key PATH instead, so both
    faults are structurally absent rather than avoided by luck.

WHAT THIS MODULE DOES AND DOES NOT DECIDE
    It resolves the spec, prints the report and maps outcomes onto exit codes.
    Every decision worth testing — what changes, whether anything changed, and
    therefore whether ``--restart`` fires — lives in the pure editor and in
    :func:`...config._env_block_line.should_restart`. A restart needs a host,
    a tmux session and a live agent; the RULE it obeys needs none of those, and
    keeping the rule out here is what lets it be tested honestly.

EXIT CODES
    ``0`` the spec now says what was asked (changed, or already correct);
    ``1`` no such agent spec, or a spec shape the editor refuses to rewrite;
    ``2`` a malformed ``KEY=VALUE`` / key name (Click's usage-error code).
"""

from __future__ import annotations

import click

from ._helpers import console


def _spec_path(name: str):
    """Resolve ``<registry>/<name>/spec.yaml``, or raise a ClickException.

    Uses ``refresh_acl._fleet_registry_dir`` — the resolver every fleet-wide
    agents verb already shares — so the ``SCITEX_AGENT_CONTAINER_AGENTS_DIR``
    override works here exactly as it does for ``refresh-acl`` and
    ``declare-a2a-host``, and the answer does not depend on the caller's cwd.
    """
    from .refresh_acl import _fleet_registry_dir

    registry = _fleet_registry_dir()
    path = registry / name / "spec.yaml"
    if not path.is_file():
        raise click.ClickException(
            f"No spec for agent {name!r}: {path} does not exist. "
            "Run `sac agents list` to see the registered agents."
        )
    return path


def _render(changes) -> None:
    """One line per key, including the no-ops.

    A key that was already at its target prints too. The command exists to be
    re-run — that is what makes it usable from a script — and a silent no-op
    is indistinguishable from a key that was never applied.
    """
    from ..config._env_block_line import ABSENT, ADDED, REMOVED, UNCHANGED, UPDATED

    style = {
        ADDED: ("green", "+"),
        UPDATED: ("green", "~"),
        REMOVED: ("yellow", "-"),
        UNCHANGED: ("dim", "="),
        ABSENT: ("dim", "="),
    }
    for change in changes:
        colour, mark = style[change.action]
        if change.action == UPDATED:
            detail = f"{change.old!r} -> {change.new!r}"
        elif change.action == UNCHANGED:
            detail = f"already {change.new!r}"
        elif change.action == ABSENT:
            detail = "not set"
        elif change.action == REMOVED:
            detail = f"was {change.old!r}"
        else:
            detail = repr(change.new)
        console.print(f"  [{colour}]{mark}[/{colour}] {change.key}: {detail}")


def _finish(ctx, name: str, path, edit, restart: bool) -> None:
    """Write when the text moved, report, and restart only if it did."""
    from ..config._env_block_line import should_restart

    if edit.refusal is not None:
        raise click.ClickException(f"{name}: refusing to edit {path} — {edit.refusal}")

    _render(edit.changes)

    if not edit.changed:
        console.print(f"[dim]{name}: already at target — {path} not written.[/dim]")
    else:
        # Written whole rather than line-patched in place: the editor returned
        # the complete document and every byte it did not intend to touch is
        # already identical.
        path.write_text(edit.text, encoding="utf-8")
        console.print(f"[green]{name}: wrote {path}[/green]")

    if not should_restart(edit, restart):
        if restart:
            console.print(f"[dim]{name}: nothing changed — not restarting.[/dim]")
        return

    from .lifecycle import restart as _restart_cmd

    console.print(f"[bold]{name}: restarting…[/bold]")
    ctx.invoke(_restart_cmd, names=(name,), yes=True)


# The docstring below is deliberately plain prose, not RST: Click renders its
# first line verbatim as the short help in `sac agents --help`, where ``double
# backticks`` would show up literally.
@click.group(name="env")
def env_group() -> None:
    """Set and unset an agent's environment variables (spec.apptainer.env)."""


@env_group.command(name="set")
@click.argument("name", metavar="NAME")
@click.argument("assignments", metavar="KEY=VALUE...", nargs=-1, required=True)
@click.option(
    "--restart",
    "restart",
    is_flag=True,
    default=False,
    help=(
        "Restart the agent afterwards — but ONLY if the spec actually "
        "changed. An agent already at the target keeps its session."
    ),
)
@click.pass_context
def env_set(ctx, name: str, assignments: "tuple[str, ...]", restart: bool) -> None:
    """Set one or more environment variables in NAME's spec.

    Idempotent: a key already holding the requested value is reported and the
    file is left byte-identical, so a re-run costs nothing and (with --restart)
    does not cost the agent its session. An existing key is replaced IN PLACE,
    keeping its position and any comment beside it; comments, blank lines and
    quote style everywhere else in the spec are preserved, because the edit is
    a line edit and not a YAML round-trip.

    The write is all-or-nothing: if any one key cannot be edited, none is.

    \b
    Example:
      $ sac agents env set foo PYTHONWARNINGS=ignore
      $ sac agents env set foo A=1 B=2 --restart
    """
    from ..config._env_block_line import MalformedAssignment, parse_assignment, set_env

    try:
        pairs = [parse_assignment(arg) for arg in assignments]
    except MalformedAssignment as exc:
        # UsageError, not ClickException: a typo in an argument is exit 2.
        raise click.UsageError(str(exc)) from exc

    path = _spec_path(name)
    _finish(ctx, name, path, set_env(path.read_text(encoding="utf-8"), pairs), restart)


@env_group.command(name="unset")
@click.argument("name", metavar="NAME")
@click.argument("keys", metavar="KEY...", nargs=-1, required=True)
@click.option(
    "--restart",
    "restart",
    is_flag=True,
    default=False,
    help=(
        "Restart the agent afterwards — but ONLY if the spec actually "
        "changed. Unsetting a key that was not set restarts nothing."
    ),
)
@click.pass_context
def env_unset(ctx, name: str, keys: "tuple[str, ...]", restart: bool) -> None:
    """Remove one or more environment variables from NAME's spec.

    A key that is not set is reported as such and is NOT an error — the point
    of the command is the end state, and that end state is already reached.
    Removing the last key leaves an explicit empty mapping rather than a
    dangling `env:`.

    \b
    Example:
      $ sac agents env unset foo PYTHONWARNINGS
    """
    from ..config._env_block_line import (
        MalformedAssignment,
        unset_env,
        validate_key,
    )

    try:
        wanted = [validate_key(key) for key in keys]
    except MalformedAssignment as exc:
        raise click.UsageError(str(exc)) from exc

    path = _spec_path(name)
    _finish(
        ctx, name, path, unset_env(path.read_text(encoding="utf-8"), wanted), restart
    )


def register(group: click.Group) -> None:
    """Attach the ``env`` sub-group to ``sac agents``."""
    group.add_command(env_group)


__all__ = ["env_group", "env_set", "env_unset", "register"]
