"""``sac check`` noun-group — preflight, health, priority checks.

Three sibling verbs (preflight / health / priority) — tree form per CLI
grammar. Old top-level names (``check`` / ``check-health`` / ``check-priority``)
remain as deprecation aliases on the registered group's siblings.

Note: the bare top-level ``check`` (preflight) is moved into ``check
preflight``; the legacy bare ``check <yaml>`` continues to work via
deprecation alias registered separately in ``_main.py``.
"""

from __future__ import annotations

import click

from .build_cmds import check as _check_preflight_impl
from .priority_cmds import priority_check as _check_priority_impl
from .status_cmds import health as _check_health_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="check")
def check_group() -> None:
    """Preflight / health / priority checks."""


check_group.add_command(_rebind(_check_preflight_impl, "preflight"))
check_group.add_command(_rebind(_check_health_impl, "health"))
check_group.add_command(_rebind(_check_priority_impl, "priority"))


__all__ = ["check_group"]
