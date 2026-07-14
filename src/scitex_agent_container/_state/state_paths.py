#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSOT for sac's OWN user-state paths — one place that knows ``$SCITEX_DIR``.

sac's user-state root is ``$SCITEX_DIR/agent-container`` (default
``~/.scitex/agent-container``). sac's own documentation already promises
this root is "relocatable via ``$SCITEX_DIR``", and ``_drift/_fleet.py``
already implements exactly that (``base="${SCITEX_DIR:-$HOME/.scitex}"``).

But that promise was kept in ONE module and broken in ~20 others, each of
which independently spelled ``Path.home() / ".scitex" / "agent-container"``
and thereby ignored ``$SCITEX_DIR`` entirely. This module is the missing
SSOT; the callers on the state-critical paths now go through it.

Why this is not cosmetic — measured on Spartan, 2026-07-14
-----------------------------------------------------------
A remote agent was launched from central control with the state root
correctly resolved from the host registry and correctly handed to the peer
as ``SCITEX_DIR=/data/gpfs/projects/punim0264/ywatanabe/.scitex``. Its
SPEC landed there. Its RUNTIME state did not — it went to
``.../paper-scitex-clew/.scitex/agent-container/runtime/`` (an unrelated
paper project) because the runtime-root constant still expanded ``~``,
and ``~/.scitex`` on that host is a symlink into that project.

Resolving the root and then ignoring it in the next module is worse than
never resolving it: it produces state SPLIT across two roots, which is
harder to reason about than state consistently in one wrong place. Hence
one helper, not another local expression of ``~``.

``$SCITEX_DIR`` unset → identical to the historical ``~/.scitex``, so
every existing local agent resolves exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "agent_container_root",
    "agents_root",
    "runtime_root",
    "user_root",
]


def user_root() -> Path:
    """``$SCITEX_DIR`` (default ``~/.scitex``) — the ecosystem user root.

    Prefers ``scitex_config``'s canonical resolver so sac agrees with every
    other scitex package by construction; falls back to reading the env var
    directly because ``scitex-config`` is an optional dependency in some
    install profiles and a missing optional dep must not break path
    resolution.

    Resolved PER CALL, never cached in a module constant: a module-level
    ``Path.home() / ".scitex"`` is frozen at import and cannot be redirected
    by an env var set afterwards — a trap this codebase has already paid
    for (a test "isolation" fixture that set ``$HOME`` and silently did
    nothing, leaving the suite reading the real fleet state).
    """
    # stx-allow: fallback (reason: scitex-config is optional in some install
    # profiles; a missing optional dep must degrade, not break path resolution)
    try:
        from scitex_config._ecosystem import local_state

        return local_state.user_root()
    except Exception:
        return Path(os.environ.get("SCITEX_DIR") or (Path.home() / ".scitex"))


def agent_container_root() -> Path:
    """``$SCITEX_DIR/agent-container`` — sac's own user-state root."""
    return user_root() / "agent-container"


def agents_root() -> Path:
    """Where agent SPECS live: ``$SCITEX_DIR/agent-container/agents``."""
    return agent_container_root() / "agents"


def runtime_root() -> Path:
    """Where agent RUNTIME state lives.

    ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` still wins when set — it is the
    explicit per-agent override the runtime already honours (and what the
    remote-launch script exports). Otherwise
    ``$SCITEX_DIR/agent-container/runtime``.
    """
    override = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    if override:
        return Path(override)
    return agent_container_root() / "runtime"


# EOF
