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
from ._explain import explain as _explain_impl
from ._helpers import HelpRecursiveGroup
from ._create import create as _create_impl
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
from .lifecycle import twin as _twin_impl
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
            ["create", "start", "twin", "stop", "restart", "delete", "forget", "spawn-from-here"],
        ),
        ("Interact", ["send", "attach"]),
        ("Inspect", ["list", "status", "health", "auth-status", "tail", "recall"]),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
        ("Account", ["accounts"]),
        ("Maintenance", ["prune-claude", "archive-claude-bloat", "refresh-acl"]),
    ]


@click.group(name="agents", cls=_AgentsGroup)
def agent_group() -> None:
    """Agent lifecycle, status, introspection, and snapshots."""


# Lifecycle verbs
# `create` scaffolds a fresh v3 spec.yaml + to_home/ skeleton (card
# sac-fresh-agent-specs, 2026-06-13). Placed FIRST in the lifecycle
# block — authoring precedes start/stop.
#
# This command was named `new` until card
# refactor/consolidate-create-into-new-templates: the OLD, narrower
# `sac agents create` (auto-detect / marker-block machinery, card
# sac-templated-agent-create 2026-06-25) was folded into `new`'s
# dir-template system, which freed the `create` name back up — `new`
# was then renamed to `create` for CRUD-consistent naming (the CLI
# already has `delete`). Use
# `sac agents create <name> --template python_developer|researcher|generalist
# --project <p>`.
agent_group.add_command(_rebind(_create_impl, "create"))
agent_group.add_command(_rebind(_start_impl, "start"))
# `twin` — spawn a context-inheriting twin of a running agent (forks the
# parent's live session, then diverges; parent never stops). See the
# twin-spawning skill + docs/adr/0019.
agent_group.add_command(_rebind(_twin_impl, "twin"))
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
# `auth-status` — prompt-anchored TUI login-stuck report (near-prompt banner +
# distance-frozen across two captures). The reliable version of the operator's
# ad-hoc auth health check; distinct from `health` (per-agent heartbeat/
# watchdog). See cli_pkg/_auth_status + _runners/_tmux/auth_status.
from ._auth_status import auth_status as _auth_status_impl  # noqa: E402

agent_group.add_command(_auth_status_impl)

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
# `refresh-acl` — re-publish every fleet agent's ACL/group policy from
# its CURRENT on-disk spec into node_comms_policy, with NO agent
# relaunch. The operator's post-restart activation step after a group-
# model change: `systemctl --user restart sac-listen && sac agents
# refresh-acl` brings the new group mesh live without a fleet relaunch.
from .refresh_acl import refresh_acl as _refresh_acl_impl  # noqa: E402

agent_group.add_command(_refresh_acl_impl)

__all__ = ["agent_group"]
