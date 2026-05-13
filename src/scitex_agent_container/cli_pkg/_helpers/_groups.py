"""Click ``Group`` subclasses + the renamed-command redirect wrapper.

``CategorizedGroup`` was historically lived in scitex_dev.click_helpers
but pinning sac's runtime to a specific scitex-dev version made
cross-repo releases brittle. Owned locally now.

``HelpRecursiveGroup`` adds the ``--help-recursive`` machinery on top of
the categorisation.

``renamed_redirect`` implements scitex CLI convention §5: renamed
commands MUST exit non-zero with a redirect message, never silently
warn-then-run. Soft warnings let stale scripts persist indefinitely;
hard errors force the fix in one iteration.
"""

from __future__ import annotations

import click


# Inline CategorizedGroup — historically lived in scitex_dev.click_helpers,
# but pinning sac's runtime to a specific scitex-dev version made cross-repo
# releases brittle. Owned locally now.
class CategorizedGroup(click.Group):
    """Click `Group` that renders `--help` commands under named sections.

    Subclass and set ``COMMAND_CATEGORIES`` as a class attribute. Categories
    are ``(section_name, [command_names])``; anything not listed falls into
    a final ``Other`` section so nothing silently disappears.
    """

    COMMAND_CATEGORIES: list = []

    def format_commands(self, ctx, formatter):
        commands = {}
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is not None and not cmd.hidden:
                commands[subcommand] = cmd
        if not commands:
            return
        displayed: set = set()
        for section, names in self.COMMAND_CATEGORIES:
            items = []
            for name in names:
                if name in commands and name not in displayed:
                    cmd = commands[name]
                    items.append((name, cmd.get_short_help_str(limit=formatter.width)))
                    displayed.add(name)
            if items:
                with formatter.section(section):
                    formatter.write_dl(items)
        leftover = [
            (n, commands[n].get_short_help_str(limit=formatter.width))
            for n in sorted(commands)
            if n not in displayed
        ]
        if leftover:
            with formatter.section("Other"):
                formatter.write_dl(leftover)


class HelpRecursiveGroup(CategorizedGroup):
    """Click group that supports --help-recursive AND categorized commands.

    Inherits categorization from `scitex_dev.click_helpers.CategorizedGroup`
    (per general/03_interface_02_cli §6). Subclasses set
    `COMMAND_CATEGORIES` (or the historical alias `command_categories` —
    see :meth:`__init_subclass__`) to opt into grouping; otherwise the
    output falls through to Click's default flat list.

    Adds the `--help-recursive` machinery on top.
    """

    # Backwards-compat alias: older sac code sets `command_categories` on
    # subclasses. Map it onto the canonical `COMMAND_CATEGORIES` slot at
    # subclass creation time so both names work.
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "command_categories" in cls.__dict__ and not cls.__dict__.get(
            "COMMAND_CATEGORIES"
        ):
            cls.COMMAND_CATEGORIES = tuple(cls.__dict__["command_categories"])

    def get_help_recursive(self, ctx) -> str:
        """Return help text for all commands recursively."""
        lines = []
        lines.append("=" * 60)
        lines.append("SciTeX Agent Container - Complete Command Reference")
        lines.append("=" * 60)
        lines.append("")

        with ctx.scope() as _:
            lines.append(self.get_help(ctx))
            lines.append("")

        for name in sorted(self.list_commands(ctx)):
            cmd = self.get_command(ctx, name)
            if cmd is None:
                continue
            lines.append("-" * 60)
            lines.append(f"Command: {name}")
            lines.append("-" * 60)
            sub_ctx = click.Context(cmd, info_name=name, parent=ctx)
            lines.append(cmd.get_help(sub_ctx))
            lines.append("")

        return "\n".join(lines)


def renamed_redirect(
    cmd: click.Command,
    *,
    new_path: str,
    old_path: str | None = None,
) -> click.Command:
    """Wrap ``cmd`` so invoking the old name hard-errors with a redirect.

    Per scitex CLI convention §5: renamed commands MUST exit non-zero
    with a redirect message, never silently warn-then-run. Soft warnings
    let stale scripts persist indefinitely; hard errors force the fix
    in one iteration.

    The wrapped command keeps its own ``params`` so ``--help`` still
    documents the surface the user invoked, but the callback is replaced:
    invoking the renamed command prints a single-line redirect to stderr
    and exits with code 2 (the convention's standard).

    Args:
        cmd: The Click command being redirected.
        new_path: The user-facing replacement (e.g. ``"sac agent start"``).
        old_path: The path the user actually typed, when it doesn't
            match ``"sac <cmd.name>"`` — typically a subcommand of a
            noun group (``"sac registry clean"`` rather than just
            ``"sac clean"``). Defaults to ``f"sac {cmd.name}"``.
    """
    rendered_old = old_path or f"sac {cmd.name}"

    def _callback(*args, **kwargs):
        del args, kwargs
        click.echo(
            f"error: '{rendered_old}' was renamed to '{new_path}'.\n"
            f"Re-run with: {new_path}",
            err=True,
        )
        raise SystemExit(2)

    return click.Command(
        name=cmd.name,
        callback=_callback,
        params=list(cmd.params),
        help=(
            (cmd.help or "")
            + f"\n\n[RENAMED] Use ``{new_path}`` instead. The old form "
            "exits with code 2."
        ),
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )
