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
from .agents_prune_claude import archive_claude_bloat as _archive_claude_bloat_impl
from .build_cmds import check as _check_impl
from .info_cmds import find as _find_impl
from .info_cmds import tail_session as _tail_impl
from .lifecycle import attach as _attach_impl
from .lifecycle import delete as _delete_impl
from .lifecycle import forget as _forget_impl
from .lifecycle import rename as _rename_impl
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
            [
                "create",
                "start",
                "twin",
                "stop",
                "restart",
                "reconcile",
                "restart-login-expired",
                "rename",
                "delete",
                "forget",
                "spawn-from-here",
            ],
        ),
        ("Interact", ["send", "attach"]),
        (
            "Inspect",
            ["list", "status", "health", "hooks", "auth-status", "tail", "recall"],
        ),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
        ("Account", ["accounts"]),
        (
            "Maintenance",
            [
                "prune-claude",
                "archive-claude-bloat",
                "refresh-acl",
                "declare-a2a-host",
                "migrate-layers",
            ],
        ),
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
# `reconcile` — the ENFORCER of "should be running => is running", and the
# only thing that ever has been. `restart: {policy: on-failure}` in ~93
# specs was dead code: the loop that reads it (`_lifecycle.health
# .health_monitor`) is launched on a daemon thread by `agent_start` and dies
# with the short-lived `sac agents start` CLI that launched it, while the
# resident listen daemon only reconciles CARDS. So when an OAuth rotation
# killed 33 agents they stayed dead until the operator noticed by chance.
# Dry-run by default; only ever restarts a CORPSE (no tmux session => no
# context to lose), never a deliberately-stopped or live-but-wedged agent.
from ._agents_reconcile import register as _register_reconcile  # noqa: E402

_register_reconcile(agent_group)
# `restart-login-expired` — the SIBLING enforcer. `reconcile` restarts DEAD
# (no tmux session) agents; this restarts LIVE ones wedged behind a frozen
# "Login expired" banner, which reconcile explicitly leaves alone (touching a
# live session destroys context). Detection is READ-ONLY + 2-run-corroborated;
# the restart is rate-limited like reconcile and goes through the pool-loading
# start path so it cannot strip CCT/Telegram tokens. DEPLOY GATE: the scheduled
# `sac.restart-login-expired-agents` timer must NOT be enabled on a host until
# that host's auth-heal.py `scan_tui` cron is retired (double-supervisor risk).
from ._agents_restart_login_expired import (  # noqa: E402
    register as _register_restart_login_expired,
)

_register_restart_login_expired(agent_group)
# `auth-audit` — READ-ONLY comparison of the shipped auth verdict against the
# pane's LAYOUT. It exists because `auth-status` flags WORKING agents: a banner
# is the last thing an agent RENDERED, not proof it is broken now, so an agent
# that 401'd, recovered and went idle stays flagged forever (verified live
# 2026-07-18 on `grant`, whose capture is checked in as a regression fixture).
# The frozen-across-two-runs hardening makes it worse, because an idle pane is
# maximally frozen. This verb counts those false positives; it NEVER restarts
# anything.
#
# It does NOT gate the restarter. An earlier draft of this comment said no
# automated restarter ships until the false-positive count is zero fleet-wide;
# the operator overruled that on 2026-07-19 — restarting a healthy agent is
# cheap, and withholding the mechanism costs more than the occasional wasted
# restart. The defect worth fixing is the PERMANENT case, where one healthy
# agent is restarted every cycle forever because a historical banner never
# leaves the pane. A tail window fixes that; a gate on this count would not.
from ._agents_auth_audit import register as _register_auth_audit  # noqa: E402

_register_auth_audit(agent_group)
# `cct-audit` — READ-ONLY sweep of the Telegram rail: which specs DECLARE
# `server:claude-code-telegrammer`, and which of them actually resolve a
# CCT_BOT_TOKEN_<SLOT>. The two are chosen independently — candidates are
# derived from the agent NAME, the pool is named by whoever wrote it — and
# nothing checked they agree, so a mismatch made an agent start perfectly,
# report healthy, and be MUTE and DEAF on Telegram (outage 2026-08-12). The
# start-time alarm closes the class going forward; this answers it for the
# agents already running, without touching one of them.
from ._agents_cct_audit import register as _register_cct_audit  # noqa: E402

_register_cct_audit(agent_group)
# `state` — the ONE state shape, returned for every agent, always. Each signal
# is True / False / None (COULD NOT DETERMINE), folded by a single pure rule
# instead of by whatever subset each call site happened to hold. It exists
# because `auth-status` and `list`, asked minutes apart on one host, returned
# DIFFERENT POPULATIONS and neither could notice. An agent it cannot read gets
# an all-None ROW rather than vanishing, and every reading is archived with its
# RAW pane captures so a verdict can be re-examined rather than merely believed.
from ._agents_state import register as _register_agents_state  # noqa: E402

_register_agents_state(agent_group)
# `deliver` — a send that reports whether it actually landed. `send` (above)
# resumes a recorded Claude session and is the right tool when the target has
# one; measured on the live host, only a handful of agents do, so for the TUI
# population it cannot deliver at all. A bare `tmux send-keys` into a session
# that does not exist prints "can't find pane" to a stderr nobody reads and
# exits 0 — which is how hours of coordination went to a session that had never
# existed, every message reported as delivered. This verb resolves and PROVES
# the target, confirms arrival by an injected token matched against a FLATTENED
# pane (a prose grep already returned 0 for a message that had arrived), and
# confirms SUBMISSION — the step that was missing, since text can sit unsent in
# the composer forever while the agent looks idle.
from ._agents_deliver import register as _register_agents_deliver  # noqa: E402

_register_agents_deliver(agent_group)
# `rename` — the ONE verb that moves an agent's name in every place it is
# written: the spec dir, the spec's own self-references (labels, workdir,
# overlay path, state-db path, and the SCITEX_TODO_AGENT_ID board
# identity), the overlay/runtime dirs, the registry entry, the state.db
# rows, AND the agent's task cards. Renaming by hand orphans the cards —
# the board still knows the agent by its old id and nothing says so.
# Atomic: every step records its inverse, any failure rolls the lot back.
agent_group.add_command(_rebind(_rename_impl, "rename"))
agent_group.add_command(_rebind(_delete_impl, "delete"))
agent_group.add_command(_rebind(_forget_impl, "forget"))
# PR-3 — in-SIF-native spawn verb with wire-stable outcome JSON +
# table-mapped exit code. Distinct from `start` (the legacy local-
# materialise-then-maybe-broker verb): `spawn-from-here` ALWAYS
# POSTs to the host listen.
from ._spawn_from_here import spawn_from_here as _spawn_from_here_impl  # noqa: E402

agent_group.add_command(_rebind(_spawn_from_here_impl, "spawn-from-here"))
# `relocate` — move an agent to a DIFFERENT HOST. The agent relocates; the host
# does not, and identity/count are unchanged (1 -> 1). Distinct from `twin`,
# which changes WHAT an agent does and takes the count to two. Dry-run only for
# now: it reports what must be true about the target BEFORE anything is touched,
# and refuses to execute while the cross-host transcript transport is unbuilt.
from ._relocate_cmd import register as _register_relocate  # noqa: E402

_register_relocate(agent_group)

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
# `migrate-layers` — step 3 of the to_home_layers migration: write into each
# spec the ``to_home`` cascade it ALREADY resolves, so what an agent inherits
# is readable from the spec instead of only derivable by re-running the
# resolver. Dry-run by default. Behaviour-preserving by construction AND by
# measurement: the apply compares what every agent ARMS before and after and
# restores every original unless they are identical over the whole population.
# Agent-spec-scoped, so it lives here rather than under a new top-level noun
# that would outlive the one-shot verb needing it.
from ._agents_migrate_layers import register as _register_migrate_layers  # noqa: E402

_register_migrate_layers(agent_group)

# `declare-a2a-host` — one-shot fleet sweep making every spec state its own
# a2a bind address instead of inheriting one from a code default. Sits beside
# `refresh-acl` because it is the same shape: reads every spec in the
# user-scope registry, safe to re-run, dry-run by default. Writes the value
# the code already falls back to, so it changes what specs SAY and not what
# agents BIND.
from ._declare_a2a_host import declare_a2a_host as _declare_a2a_host_impl  # noqa: E402

agent_group.add_command(_declare_a2a_host_impl)

# `hooks` — what Claude Code hooks are ACTUALLY armed in this container, and
# does the agent meet the floor its spec declares? Read-only, and measured from
# INSIDE the container because every host-side proxy for the two stacked home
# mounts has undercounted (67 vs 71 on 2026-08-10). The same command is what
# `runtimes._apptainer_inner_argv` runs on the container's own boot path for a
# spec that declares a floor, so the gate's verdict is reproducible by hand.
from ._agents_hooks import register as _register_hooks  # noqa: E402

_register_hooks(agent_group)

__all__ = ["agent_group"]
