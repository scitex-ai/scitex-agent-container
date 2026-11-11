"""Expose native policy schemas to Click discovery without a second parser.

The argparse provider retains JSON errors, exit codes and execution. Click
metadata is derived from that same provider so agents can inspect operations,
required options and approval controls instead of an unprocessed argv leaf.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import click
from scitex_dev.ecosystem import CliHelp, Example, SpecCommand, SpecGroup

from ._cli import build_parser, main

_EXAMPLES = {
    "inspect": "inspect --repo .",
    "assert-context": "assert-context --repo . --intent read",
    "check-edit": "check-edit --path ./example.py",
    "check-shell": "check-shell --cwd . --command 'git status'",
    "generate-projections": "generate-projections --dry-run",
    "check-projections": "check-projections",
}


class _NativePolicy:
    """Keep native validation, including typed argument errors with exit 3."""

    operation: str | None = None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["sac_policy_argv"] = tuple(args)
        return []

    def invoke(self, ctx: click.Context) -> None:
        argv = list(ctx.meta["sac_policy_argv"])
        prog = ctx.command_path
        if self.operation is not None:
            argv.insert(0, self.operation)
            prog = ctx.parent.command_path if ctx.parent else "worktree-policy"
        raise SystemExit(main(argv, prog=prog))


class PolicyCommand(_NativePolicy, SpecCommand):
    """A discoverable native operation, also callable as a Click command."""


class PolicyGroup(_NativePolicy, SpecGroup):
    """The native policy namespace with provider-derived children and options."""


def _options(parser: argparse.ArgumentParser) -> list[click.Option]:
    result = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if not action.option_strings:
            raise ValueError(f"Unsupported policy discovery action: {action.dest}")
        kwargs = {
            "required": action.required,
            "default": action.default,
            "help": action.help,
        }
        if isinstance(action, argparse._StoreTrueAction) and action.nargs == 0:
            kwargs.update(is_flag=True, flag_value=True)
        elif (
            isinstance(action, argparse._StoreAction)
            and action.nargs in (None, 1)
            and action.type
            in (
                None,
                str,
                Path,
            )
        ):
            kwargs["type"] = (
                click.Choice(action.choices)
                if action.choices is not None
                else click.Path(path_type=Path)
                if action.type is Path
                else click.STRING
            )
        else:
            raise ValueError(f"Unsupported policy discovery type: {action.dest}")
        option = click.Option(action.option_strings, **kwargs)
        if option.name != action.dest:
            raise ValueError(f"Policy option name differs from provider: {action.dest}")
        result.append(option)
    return result


def _help(parser: argparse.ArgumentParser, example: str) -> CliHelp:
    summary, separator, description = parser.description.partition(". ")
    return CliHelp(
        summary=summary + ("." if separator else ""),
        description=description,
        examples=(Example("{prog} worktree policy " + example),),
        exit_codes={
            0: "Result or preview printed.",
            2: "Policy denies the request.",
            3: "Invalid arguments or policy.",
            4: "Git inspection failed.",
        },
        version_of="scitex-agent-container",
    )


def build_policy_group() -> PolicyGroup:
    """Describe exactly the operations accepted by the native policy parser."""
    parser = build_parser()
    subparsers = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if len(subparsers) != 1:
        raise ValueError("Policy discovery requires one native operation namespace")
    group = PolicyGroup(
        "policy",
        params=_options(parser),
        help_spec=_help(parser, "inspect --repo ."),
        command_categories=[("Core", tuple(subparsers[0].choices))],
    )
    for name, child in subparsers[0].choices.items():
        command = PolicyCommand(
            name, params=_options(child), help_spec=_help(child, _EXAMPLES[name])
        )
        command.operation = name
        group.add_command(command)
    return group
