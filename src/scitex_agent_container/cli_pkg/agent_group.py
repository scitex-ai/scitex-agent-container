"""``sac agents`` noun-group — every agent-scoped operation.

Plural form. Renamed from ``sac agent`` so the verb shape lines up
with the list-of-things commands underneath (``start NAME...``,
``stop NAME...``, ``delete NAME...``, ``tail NAME...``).

``accounts`` lives at the top level (``sac accounts``); the credential
store is fleet-wide, not agent-scoped.
"""

from __future__ import annotations

import click

from ._agent_prune_claude import prune_claude as _prune_claude_impl
from ._create import create as _create_impl
from ._explain import explain as _explain_impl
from ._helpers import HelpRecursiveGroup
from ._new import new as _new_impl
from .agents_prune_claude import archive_claude_bloat as _archive_claude_bloat_impl
from .build_cmds import check as _check_impl
from .info_cmds import find as _find_impl
from .info_cmds import tail_session as _tail_impl
from .lifecycle import attach as _attach_impl
from .lifecycle import delete as _delete_impl
from .lifecycle import forget as _forget_impl
from .lifecycle import restart as _restart_impl
from .lifecycle import start as _start_impl
from .lifecycle import stop as _stop_impl
from .recall_cmds import recall as _recall_impl
from .send_cmds import send as _send_impl
from .status_cmds import health as _health_impl
from .status_cmds import status as _status_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


class _AgentsGroup(HelpRecursiveGroup):
    """Render ``sac agents --help`` with grouped sections instead of one
    flat alphabetical list."""

    COMMAND_CATEGORIES = [
        (
            "Lifecycle",
            ["new", "create", "start", "stop", "restart", "delete", "forget", "spawn-from-here"],
        ),
        ("Interact", ["send", "attach"]),
        ("Inspect", ["list", "status", "health", "tail", "recall"]),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
        ("Account", ["accounts"]),
        ("Maintenance", ["prune-claude", "archive-claude-bloat"]),
    ]


@click.group(name="agents", cls=_AgentsGroup)
def agent_group() -> None:
    """Agent lifecycle, status, introspection, and snapshots."""


# Lifecycle verbs
# `new` scaffolds a fresh v3 spec.yaml + to_home/ skeleton (card
# sac-fresh-agent-specs, 2026-06-13). Placed FIRST in the lifecycle
# block — authoring precedes start/stop.
agent_group.add_command(_rebind(_new_impl, "new"))
# `create` stamps a proven-shape developer/scientist agent from the
# underscore-agent skeletons (card sac-templated-agent-create, 2026-06-25)
# — folds the retired new_agent_spec.sh / gen_ecosystem_dev_specs.sh
# stampers. `new` is the bare scaffold; `create` is the opinionated,
# auto-detecting proven shape.
agent_group.add_command(_rebind(_create_impl, "create"))
agent_group.add_command(_rebind(_start_impl, "start"))
agent_group.add_command(_rebind(_stop_impl, "stop"))
agent_group.add_command(_rebind(_restart_impl, "restart"))
agent_group.add_command(_rebind(_delete_impl, "delete"))
agent_group.add_command(_rebind(_forget_impl, "forget"))
# PR-3 — in-SIF-native spawn verb with wire-stable outcome JSON +
# table-mapped exit code. Distinct from `start` (the legacy local-
# materialise-then-maybe-broker verb): `spawn-from-here` ALWAYS
# POSTs to the host listen.
from ._spawn_from_here import spawn_from_here as _spawn_from_here_impl  # noqa: E402

agent_group.add_command(_rebind(_spawn_from_here_impl, "spawn-from-here"))

# Polysemous noun-leaves (allowed under noun groups by §1 loosening)
agent_group.add_command(_rebind(_status_impl, "list"))
# `status` is a muscle-memory alias for `list` — the top-level CLI
# help text + the README example call out `sac agents status`, and
# operators expect it to exist (foundation-polish bug 2).
agent_group.add_command(_rebind(_status_impl, "status"))
agent_group.add_command(_rebind(_tail_impl, "tail"))
agent_group.add_command(_rebind(_health_impl, "health"))

# Verb leaves
agent_group.add_command(_rebind(_find_impl, "find"))
agent_group.add_command(_rebind(_recall_impl, "recall"))
agent_group.add_command(_rebind(_check_impl, "check"))
agent_group.add_command(_rebind(_send_impl, "send"))
# `attach` — hand the terminal to a running agent's TUI (tmux) session.
agent_group.add_command(_rebind(_attach_impl, "attach"))
# `explain` — render the FULL effective launch plan (mounts, --pwd, env,
# skills, prompts) WITHOUT launching, so the caller sees exactly what
# `start` will do (No-Surprise). Already named; no _rebind needed.
agent_group.add_command(_explain_impl)
# F-CS8 prune — dry-run-by-default purge of the two known workdir
# bloat sources (.pending/ records + merged-only worktrees/agent-*).
agent_group.add_command(_rebind(_prune_claude_impl, "prune-claude"))
# F-CS8 audit-driven archive — closes the start-hook banner-to-action
# loop by moving every audit ``bloat_sources`` entry to
# ``<workdir>/.claude/.archived-<UTC>/<rel_path>/``. Complements
# ``prune-claude`` (the narrower, dry-run-first, two-bucket scheme):
# this command is the wide audit-driven button.
agent_group.add_command(_rebind(_archive_claude_bloat_impl, "archive-claude-bloat"))

__all__ = ["agent_group"]
