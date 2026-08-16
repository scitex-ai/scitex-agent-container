#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_system_deps.py
"""scitex-agent-container's own SYSTEM (apt) dependency declarations.

sac declared none until now, while ``apptainer-base.def`` hardcoded an apt
list — so the BASE image bypassed the very federation the SCITEX def calls
"SSoT — NOT a hardcoded list". This registers sac under the same
``scitex_dev.system_deps`` entry-point group every leaf uses, so
``discover_system_deps`` aggregates it like any other provider.

THE REASON FIELD IS THE POINT, not the package list. An apt line records
WHAT is installed; only the reason records WHY, and "why" is what makes the
list auditable — it is how a future reader decides whether a dep can be
dropped. Each entry below names the mechanism that breaks without it, not
the tool's marketing description.

Note especially the search tools: they are not conveniences here. The
agent hooks this fleet runs MANDATE ripgrep (``enforce_ripgrep.sh`` denies
``grep -r`` outright), so an undeclared mandate is one apt-line edit away
from silently breaking every agent's search. That is exactly the class of
coupling a declaration exists to make visible.
"""

from __future__ import annotations

from scitex_dev.system_deps import SystemDepSpec

_PROVIDER = "scitex-agent-container"


def provide() -> list[SystemDepSpec]:
    """System deps sac itself needs at image-build time."""
    return [
        SystemDepSpec(
            "ripgrep",
            "MANDATED by the agent hooks: enforce_ripgrep.sh DENIES `grep -r` "
            "and instructs the agent to use `rg`, so removing it breaks every "
            "agent's search rather than degrading it",
            _PROVIDER,
        ),
        SystemDepSpec(
            "fd-find",
            "the hook-sanctioned `find` replacement agents are steered to; "
            "paired with ripgrep in the same enforcement doctrine",
            _PROVIDER,
        ),
        SystemDepSpec(
            "gh",
            "agents open their own pull requests (operator ruling 2026-08-09); "
            "`gh pr create` is an API call, so SSH transport alone is not "
            "enough. Present in the container image and deliberately absent "
            "from the bare host — the host runs the runtime, the container "
            "runs the tooling",
            _PROVIDER,
        ),
        SystemDepSpec(
            "git",
            "the worktree-per-task workflow is enforced by hooks: edits are "
            "denied outside a linked worktree, so git is load-bearing for the "
            "agent to do any tracked-file work at all",
            _PROVIDER,
        ),
        SystemDepSpec(
            "tmux",
            "an agent session IS a tmux session — sac's TuiSessionRuntime "
            "starts, stops and injects turns through it, and tmux is also the "
            "only liveness signal that survives a registry outage",
            _PROVIDER,
        ),
        SystemDepSpec(
            "openssh-client",
            "cross-host credential distribution and peer snapshot push "
            "(_account.snapshot_push) run over ssh; without it a compute host "
            "cannot pull a refreshed token from the master",
            _PROVIDER,
        ),
        SystemDepSpec(
            "curl",
            "bootstrap path for the uv installer in the container entrypoint, "
            "and the fallback probe for the listen daemon's health endpoint",
            _PROVIDER,
        ),
        SystemDepSpec(
            "postgresql-client",
            "the cards/fleet store IS PostgreSQL and the image already gates "
            "its PYTHON driver (psycopg); psql is the other half — the one a "
            "person or an agent uses to look at the store. Measured 2026-08-13: "
            "asked how the fleet DB is organised, this container had no psql "
            "and could only answer from general knowledge instead of from the "
            "database in front of it",
            _PROVIDER,
        ),
    ]


__all__ = ["provide"]
