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


def _resolve_account(config: AgentConfig | None) -> str:
    """Resolve the agent's effective Anthropic-account label.

    Surfaces which account the agent authenticates as (operator request
    4581) so the operator can see which agents share one account — and
    thus one server-side rate limit. Mirrors the runtime auth precedence
    (agent ``spec.env`` override → host shared OAuth → fallback). See
    ``_account.agent_account.resolve_agent_account_label``.

    Tolerant: a missing config or any resolver hiccup maps to
    ``"unknown"`` so status never fails on account lookup.
    """
    # stx-allow: fallback (reason: status output must never crash on an
    # account-resolution hiccup; ``"unknown"`` is the right degraded UX.)
    try:
        from .._account.agent_account import resolve_agent_account_label

        env = config.env if config is not None else None
        assigned = (
            getattr(getattr(config, "claude", None), "account", "") or None
            if config is not None
            else None
        )
        return resolve_agent_account_label(env, assigned_account=assigned)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return "unknown"


def _remote_instance_status(name: str) -> dict | None:
    """Build a status dict from the active ``instances`` row for ``name``.

    Used when the LOCAL file registry has no entry — the case for a
    cross-host-dispatched agent, whose row was written into the
    ``instances`` table by the dispatcher (``remote=1`` + peer ``host``
    + peer-resolved ``bound_port``). Returns ``None`` when no active row
    exists (caller raises the normal "not found" error), or on any
    lookup failure.

    The shape mirrors the canonical ``agent_status`` keys callers
    expect, surfacing the family-tree fields (``host``, ``a2a_port`` /
    ``bound_port``, ``remote``, ``spawned_by``) so a remote agent
    resolves rather than erroring.
    """
    try:
        from .._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r.get("name") == name]
        if not rows:
            return None
        # list_active_instances orders started_at DESC → newest first.
        row = rows[0]
        bound = row.get("bound_port")
        if bound is None:
            bound = row.get("a2a_port")
        return {
            "name": name,
            "config": "",
            "screen": row.get("screen", "") or "",
            "started_at": row.get("started_at", "") or "",
            # The instances row says the agent is active (ended_at IS
            # NULL); reaching the remote runtime to confirm is the
            # cross-host dispatcher's job, not this read-side resolver.
            "status": "running",
            "model": "unknown",
            "runtime": "unknown",
            # Cross-host agent: its credentials live on the remote host,
            # not resolvable from here. Keep the key for shape parity.
            "account": "unknown",
            "host": row.get("host", "") or "",
            "a2a_port": row.get("a2a_port"),
            "bound_port": bound,
            "remote": bool(row.get("remote")),
            "spawned_by": row.get("spawned_by"),
        }
    except Exception:  # stx-allow: fallback (reason: best-effort cross-host status — caller raises the normal "not found" error when None)
        return None


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
        # Cross-host fallback (sac-agent-spawn design, Rule B/F): a
        # remote-dispatched agent has no LOCAL file-registry entry — its
        # row lives in the ``instances`` table written by the cross-host
        # dispatcher. Resolve status from there so ``sac agents status
        # <remote>`` reports host + bound_port + remote + spawned_by
        # instead of raising "not found in registry".
        remote_status = _remote_instance_status(name)
        if remote_status is not None:
            return remote_status
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
        # Which Anthropic account this agent authenticates as (operator
        # request 4581). Agents sharing one label share one server-side
        # rate limit. Resolved from the agent's effective auth source.
        "account": _resolve_account(config),
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

    # Lead task 2026-06-01: per-agent CPU% + RSS in the status JSON. The
    # list command does its own batched probe across all rows; for the
    # single-agent status path we probe one PID directly. Same module
    # (``_state._meta.resources.collect_agent_resources``), same
    # observability contract (absent ≠ 0, dead PID → no fields).
    # stx-allow: fallback (reason: psutil is an optional dependency;
    # absence (or any per-process probe failure) absent-outs the row
    # rather than crashing status — same shape as the host metrics block.)
    try:
        from .._state._meta.resources import collect_agent_resources

        _pid_for_probe = entry.get("pid") or 0
        if _pid_for_probe:
            _agent_res = collect_agent_resources([_pid_for_probe]).get(_pid_for_probe)
            if _agent_res is not None:
                result["cpu_percent"] = _agent_res["cpu_percent"]
                result["mem_rss_mb"] = _agent_res["mem_rss_mb"]
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass

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
