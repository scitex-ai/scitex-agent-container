"""``agent_status`` / ``agent_logs`` — read-side lifecycle queries.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ._runtime_select import _fallback_workdir, _get_runtime


def agent_status(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> dict:
    """Get detailed status for an agent.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    # stx-allow: fallback (reason: YAML or runtime may be unavailable; status should degrade to stopped=False rather than raise)
    try:
        config = load_config(entry["config"])
        runtime = runtime_factory(config)
        running = runtime.is_running(config)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        traceback.print_exc()
        running = False
        config = None

    result = {
        "name": name,
        "config": entry.get("config", ""),
        "screen": entry.get("screen", ""),
        "started_at": entry.get("started_at", ""),
        "status": "running" if running else "stopped",
        "model": config.model if config else "unknown",
        "runtime": config.runtime if config else "unknown",
    }
    # ``config.remote`` was deleted in WI-6; spec.host (host pinning)
    # is the v3 equivalent and is recorded in state.db's ``instances``
    # table rather than echoed back through ``status``.

    # Hook-points / listen / extensions plumbing (todo#286 Phase 4).
    # Counts are exposed so consumers can see what's wired up; command
    # bodies are intentionally NOT echoed to avoid leaking URLs or
    # secrets through status --json.
    if config is not None:
        hooks = config.hooks or {}
        result["hooks_configured"] = {
            key: len(hooks.get(key, []) or [])
            for key in (
                "pre_start",
                "post_start",
                "pre_stop",
                "post_stop",
                "on_compact",
                "on_restart",
                "on_diff",
            )
        }
        result["listen"] = [
            {
                "port": lp.port,
                "proto": lp.proto,
                "path": lp.path,
                "name": lp.name,
                "owner": lp.owner,
            }
            for lp in (config.listen or [])
        ]
        # Opaque pass-through — echoed verbatim.
        result["extensions"] = dict(config.extensions or {})
    else:
        result["hooks_configured"] = {}
        result["listen"] = []
        result["extensions"] = {}

    result["context_management"] = None

    # Snapshot block — cheap read from cache (todo#286). Never re-gathers.
    # stx-allow: fallback (reason: snapshot module may not yet exist or cache may be absent on first run; None snapshot is valid initial state)
    try:
        from .._state.snapshot import read_latest

        latest = read_latest(name)
        if latest is not None:
            result["snapshot"] = {
                "timestamp": latest.get("timestamp"),
                "has_diff": latest.get("has_diff", False),
                "diff_fields": latest.get("diff_fields", []),
            }
        else:
            result["snapshot"] = None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        result["snapshot"] = None

    # Enrich with claude-hud-style metadata. Canonical source for the
    # Agents-tab dashboard; the MCP sidecar heartbeat shells out to this
    # command rather than duplicating the logic in TypeScript.
    # stx-allow: fallback (reason: agent_meta requires psutil and an active tmux session; metadata enrichment is optional and must never break status)
    try:
        from .._state.agent_meta import collect_rich

        workdir = config.expanded_workdir if config else _fallback_workdir(name)
        session = entry.get("screen", "") or (config.screen_name if config else name)
        rich = collect_rich(name=name, workdir=workdir, session=session)
        # Prefer transcript-derived started_at only if the registry
        # doesn't have one.
        if not result.get("started_at") and rich.get("started_at_transcript"):
            result["started_at"] = rich["started_at_transcript"]
        rich.pop("started_at_transcript", None)
        rich.pop("model_transcript", None)
        # Never let rich overwrite the canonical registry/config fields.
        for k, v in rich.items():
            result.setdefault(k, v)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        # Never let metadata collection break status.
        pass

    return result


def agent_logs(
    name: str,
    lines: int = 50,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> str:
    """Get recent logs from an agent.

    Args:
        name: Agent name.
        lines: Number of trailing log lines to return.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    config = load_config(entry["config"])
    runtime = runtime_factory(config)
    return runtime.logs(config, lines)
