"""LazyGroup — defer subcommand imports until invocation.

Click's normal pattern is to ``from .foo_cmds import foo_command`` at
the top of the entry-point module so every subcommand is registered
before ``main()`` runs. With ~30 subcommand modules each pulling in
their own dependencies (rich, runtimes, a2a, _lifecycle, …), the
import graph balloons to ~2 s of cold-start cost on every invocation
— including every ``<TAB>`` press in the user's shell.

``LazyGroup`` keeps ``list_commands`` cheap by enumerating names from
a small ``{name: "module.path:symbol"}`` registry, and only imports a
subcommand's module when ``get_command(name)`` is actually called
(invocation, ``--help <name>``, dispatch). Renamed-command redirects
work the same way: their underlying module is only imported if the
user actually types the legacy name.

Usage:

    class MyGroup(LazyGroup):
        LAZY_COMMANDS = {"agent": "myproj.cli.agent_group:agent_group"}
        LAZY_RENAMED  = {"start": ("myproj.cli.lifecycle:start", "myproj agent start")}

The class still inherits from ``HelpRecursiveGroup`` so categorization
and ``--help-recursive`` keep working. ``--help-recursive`` does pay
the full import cost — that's the explicit "I want everything" path.
"""

from __future__ import annotations

import importlib

import click

# Import directly from the submodule — going through ``_helpers/__init__``
# would trigger the re-export shim and eager-load ``_agent_list`` (which
# pulls config + rich.table + scitex_logging, +60 ms cold). The lazy
# group is on the cold-start path of every `sac` invocation — keep it lean.
from ._helpers._groups import HelpRecursiveGroup, renamed_redirect


class _LazyCommandsDict(dict):
    """Dict-of-commands that resolves lazy entries on ``get`` / ``in``.

    The CLI audit (and any third-party tooling that introspects a
    click group) reads ``group.commands.get(name)`` directly without
    going through ``Group.get_command``. With a plain dict, lazy
    entries are invisible. This subclass forwards misses to the owning
    group's lazy resolver and caches the result.
    """

    def __init__(self, group: "LazyGroup") -> None:
        super().__init__()
        self._group = group

    def _try_resolve(self, name: str) -> click.Command | None:
        if dict.__contains__(self, name):
            return dict.__getitem__(self, name)
        cmd = self._group._resolve_lazy(name)
        if cmd is not None:
            dict.__setitem__(self, name, cmd)
        return cmd

    def get(self, name, default=None):
        cmd = self._try_resolve(name)
        return cmd if cmd is not None else default

    def __contains__(self, name) -> bool:
        return self._try_resolve(name) is not None

    def __getitem__(self, name):
        cmd = self._try_resolve(name)
        if cmd is None:
            raise KeyError(name)
        return cmd


class LazyGroup(HelpRecursiveGroup):
    """Click group that defers subcommand imports until invocation.

    Subclasses set ``LAZY_COMMANDS`` (regular subcommands) and
    ``LAZY_RENAMED`` (legacy aliases that redirect to a new path).
    Both map a user-facing name to a ``"module.path:symbol"`` spec
    that's only resolved on demand.

    Eagerly-registered commands (via ``add_command``) still work and
    take precedence over lazy entries with the same name.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Replace the plain dict click set in MultiCommand.__init__ so
        # ``self.commands.get(name)`` from external introspection
        # (audit-cli, scripts, …) sees lazy entries too.
        self.commands = _LazyCommandsDict(self)

    # Map: user-facing name → "module.path:attribute"
    LAZY_COMMANDS: dict[str, str] = {}

    # Map: legacy_name → ("module.path:attribute", "new user-facing path")
    # Resolved lazily; the legacy name resolves to a hidden
    # renamed_redirect wrapper that prints "renamed to X" and exits.
    LAZY_RENAMED: dict[str, tuple[str, str]] = {}

    # Optional: short_help cache for ``--help`` rendering. Click's
    # default ``format_commands`` calls ``get_short_help_str()`` on
    # every command — that would force a full import per row, defeating
    # the laziness. With ``LAZY_SHORT_HELPS`` populated, the formatter
    # below renders straight from the cache. Names not in the cache
    # fall back to the per-command lookup (importing as needed).
    LAZY_SHORT_HELPS: dict[str, str] = {}

    def _import(self, spec: str) -> click.Command:
        module_path, attr = spec.rsplit(":", 1)
        module = importlib.import_module(module_path)
        return getattr(module, attr)

    def _resolve_lazy(self, name: str) -> click.Command | None:
        """Materialise a lazy entry to a real Command, or return None.

        Looks up the name in ``LAZY_COMMANDS`` first, then ``LAZY_RENAMED``.
        Used by ``get_command`` and by ``_LazyCommandsDict`` so external
        introspection of ``group.commands`` sees lazy entries.
        """
        spec = self.LAZY_COMMANDS.get(name)
        if spec is not None:
            return self._import(spec)
        renamed = self.LAZY_RENAMED.get(name)
        if renamed is not None:
            spec, new_path = renamed
            wrapped = renamed_redirect(self._import(spec), new_path=new_path)
            wrapped.name = name
            wrapped.hidden = True
            return wrapped
        return None

    def list_commands(self, ctx: click.Context) -> list[str]:
        # Renamed aliases are hidden from completion + help by design,
        # so we omit them from list_commands. Eager + lazy regular
        # commands are merged and deduplicated.
        eager = set(super().list_commands(ctx))
        lazy = set(self.LAZY_COMMANDS)
        return sorted(eager | lazy)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # The ``_LazyCommandsDict`` installed in ``__init__`` already
        # resolves lazy entries on ``commands.get``, so the parent's
        # ``Group.get_command`` (which does ``self.commands.get``)
        # transparently handles both eager and lazy paths.
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx, formatter):
        # Re-implement the categorized formatter so we don't call
        # ``get_command`` (and trigger an import) for entries we have a
        # cached short_help for. Names not in the cache fall back to
        # the per-command lookup (importing as needed).
        if not self.LAZY_SHORT_HELPS:
            return super().format_commands(ctx, formatter)

        names = self.list_commands(ctx)
        cached = self.LAZY_SHORT_HELPS

        short_helps: dict[str, str] = {}
        for name in names:
            if name in cached:
                short_helps[name] = cached[name]
                continue
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            short_helps[name] = cmd.get_short_help_str(limit=formatter.width)

        if not short_helps:
            return

        displayed: set[str] = set()
        for section, section_names in self.COMMAND_CATEGORIES:
            items = [
                (n, short_helps[n])
                for n in section_names
                if n in short_helps and n not in displayed
            ]
            for n, _ in items:
                displayed.add(n)
            if items:
                with formatter.section(section):
                    formatter.write_dl(items)
        leftover = [
            (n, short_helps[n]) for n in sorted(short_helps) if n not in displayed
        ]
        if leftover:
            with formatter.section("Other"):
                formatter.write_dl(leftover)
        return None


__all__ = ["LazyGroup"]
