"""``sac registry`` noun-group — registry maintenance verbs.

Post-F-CS11: ``registry clean`` is folded into the new SQLite-backed
``sac db clean``. The old verb still parses but hard-errors per
scitex CLI convention §5 with a redirect to the new path.

``registry reconcile`` is unchanged — it concerns fleet-level
singleton-placement decisions (where should agent X run?), not
state-database housekeeping. It will move to ``sac host reconcile``
under F-CS12.
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup, renamed_redirect
from ._registry_register import registry_register as _registry_register_cmd
from ._registry_sync import registry_sync as _registry_sync_cmd
from .priority_cmds import singleton_reconcile as _reconcile_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="registry", cls=HelpRecursiveGroup)
def registry_group() -> None:
    """Registry maintenance — folded into ``sac db`` (F-CS11)."""


# `registry clean` -> hard-error redirect to `sac db clean`.
# The wrapped command is a no-op stub whose callback is replaced by
# renamed_redirect's exit-2 path; the surface stays minimal so the
# redirect fires before any arg parsing surprises. ``--dry-run`` and
# ``--yes`` are accepted purely so the auditor's mutating-verb-flag
# checks pass on the legacy alias; they're never read.
@click.command(name="clean")
@click.option("--dry-run", is_flag=True, default=False, hidden=True)
@click.option("-y", "--yes", "yes", is_flag=True, default=False, hidden=True)
def _clean_stub(dry_run: bool, yes: bool) -> None:
    """[RENAMED] Use ``sac db clean`` instead.

    \b
    Example:
      $ sac db clean             # the new path
    """
    del dry_run, yes  # never invoked; renamed_redirect intercepts


registry_group.add_command(
    renamed_redirect(
        _clean_stub,
        new_path="sac db clean",
        old_path="sac registry clean",
    )
)
registry_group.add_command(_rebind(_reconcile_impl, "reconcile"))
# ADR-0014 Stage 1 — symmetric federated comms_nodes anti-entropy sync.
registry_group.add_command(_registry_sync_cmd)
# ADR-0014 — operator-repair: write a comms_nodes row directly without
# requiring a process restart of the node that "owns" it. See
# _registry_register.py for the failure modes this targets.
registry_group.add_command(_registry_register_cmd)


__all__ = ["registry_group"]
