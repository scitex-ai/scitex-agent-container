"""Agent-aware ``--profile`` help and shell completion for ``agents start``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import click
import yaml
from click.shell_completion import CompletionItem

from ...config import resolve_config

_RAW_ARGS_ATTRIBUTE = "_sac_start_active_raw_args"
_PROFILE_HELP = (
    "Select a named launch profile (defaults to spec.default_profile)."
)


@dataclass(frozen=True)
class ProfileCatalog:
    """Named profiles declared by one raw spec, before secret resolution."""

    target: str
    names: tuple[str, ...]
    default: str


def profile_catalog_for_target(target: str) -> ProfileCatalog | None:
    """Read the profile envelope for ``target`` without loading its runtime."""
    try:
        path = Path(resolve_config(target))
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        return None
    profiles = spec.get("profiles")
    default = spec.get("default_profile")
    if not isinstance(profiles, dict) or not profiles:
        return None
    names = tuple(str(name) for name in profiles)
    if not isinstance(default, str) or default not in profiles:
        return None
    return ProfileCatalog(target=target, names=names, default=default)


def profile_name_complete(
    ctx: click.Context, _param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete profiles from the already-parsed single target."""
    targets = tuple(ctx.params.get("targets") or ())
    if len(targets) != 1:
        return []
    catalog = profile_catalog_for_target(str(targets[0]))
    if catalog is None:
        return []
    return [
        CompletionItem(
            name,
            help="default profile" if name == catalog.default else "launch profile",
        )
        for name in catalog.names
        if name.startswith(incomplete)
    ]


def _option_lookup(command: click.Command) -> dict[str, click.Option]:
    lookup: dict[str, click.Option] = {}
    for param in command.params:
        if isinstance(param, click.Option):
            for spelling in (*param.opts, *param.secondary_opts):
                lookup[spelling] = param
    return lookup


def _positional_tokens(command: click.Command, raw_args: Sequence[str]) -> list[str]:
    """Extract positional tokens sufficiently for agent-specific help."""
    options = _option_lookup(command)
    positionals: list[str] = []
    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token == "--":
            positionals.extend(raw_args[index + 1 :])
            break
        option_name = token.split("=", 1)[0]
        option = options.get(option_name)
        if option is not None:
            index += 1
            if "=" not in token and not option.is_flag and not option.count:
                index += option.nargs
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def _agent_specific_profile_help(
    command: click.Command, raw_args: Sequence[str]
) -> str:
    targets = _positional_tokens(command, raw_args)
    if len(targets) != 1:
        return _PROFILE_HELP
    catalog = profile_catalog_for_target(targets[0])
    if catalog is None:
        return _PROFILE_HELP
    rendered = ", ".join(
        f"{name} (default)" if name == catalog.default else name
        for name in catalog.names
    )
    return f"{_PROFILE_HELP} Available for {catalog.target}: {rendered}."


class StartCommand(click.Command):
    """Click command whose help can inspect the raw target before ``--help``."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: object,
    ) -> click.Context:
        previous = getattr(self, _RAW_ARGS_ATTRIBUTE, ())
        setattr(self, _RAW_ARGS_ATTRIBUTE, tuple(args))
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        finally:
            setattr(self, _RAW_ARGS_ATTRIBUTE, previous)

    def format_options(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        profile_option = next(
            (
                param
                for param in self.params
                if isinstance(param, click.Option) and "--profile" in param.opts
            ),
            None,
        )
        if profile_option is None:
            super().format_options(ctx, formatter)
            return
        original_help = profile_option.help
        raw_args = tuple(getattr(self, _RAW_ARGS_ATTRIBUTE, ()))
        profile_option.help = _agent_specific_profile_help(self, raw_args)
        try:
            super().format_options(ctx, formatter)
        finally:
            profile_option.help = original_help


__all__ = [
    "ProfileCatalog",
    "StartCommand",
    "profile_catalog_for_target",
    "profile_name_complete",
]
