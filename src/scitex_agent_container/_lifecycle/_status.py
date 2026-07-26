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
            "profile": row.get("profile") or "unknown",
            "harness": row.get("harness") or "unknown",
            "backend": row.get("backend") or "unknown",
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


def _liveness_block(
    name: str,
    config: AgentConfig | None,
    runtime_factory: Optional[Callable[[AgentConfig], Any]],
) -> dict:
    """The ternary liveness verdict + its evidence, for ``agent_status``.

    Tolerant by construction: any failure to gather degrades to an UNKNOWN
    verdict with the reason attached — never to a fabricated DEAD, and never to
    an exception that takes the whole status command down with it.
    """
    from ._verdict import (
        INSTRUMENT_NO_OBSERVATION,
        SOURCE_RESOLVER,
        UNKNOWN,
        LivenessVerdict,
        Signal,
    )
    from ._verdict_resolve import resolve_verdict

    try:
        runtime = None
        if config is not None:
            factory = runtime_factory or _get_runtime
            runtime = factory(config)
        return resolve_verdict(name, config, runtime).to_dict()
    except Exception as exc:  # stx-allow: fallback (reason: an un-gatherable verdict is UNKNOWN with its reason — never a fabricated DEAD, and never a crashed status command)
        return LivenessVerdict(
            agent=name,
            verdict=UNKNOWN,
            signals=(
                Signal(
                    SOURCE_RESOLVER,
                    UNKNOWN,
                    f"could not gather liveness evidence ({type(exc).__name__}: {exc})",
                    INSTRUMENT_NO_OBSERVATION,
                ),
            ),
        ).to_dict()


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
        config = load_config(entry["config"], profile=entry.get("profile"))
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
        "profile": (
            config.profile if config else entry.get("profile", "unknown")
        ),
        "harness": (
            config.harness if config else entry.get("harness", "unknown")
        ),
        "backend": (
            config.backend if config else entry.get("backend", "unknown")
        ),
        # Which Anthropic account this agent authenticates as (operator
        # request 4581). Agents sharing one label share one server-side
        # rate limit. Resolved from the agent's effective auth source.
        "account": _resolve_account(config),
    }

    # TERNARY liveness verdict + THE EVIDENCE FOR IT.
    #
    # ``status`` above is the legacy BOOL ("running"/"stopped"), and it cannot
    # say "I don't know" — so it says one of the two poles and the reader has no
    # way to tell a confident verdict from a coin-flip. An operator staring at
    # ``running | pid=None`` learns nothing at all.
    #
    # ``liveness`` says WHICH and WHY: "ALIVE (delivery: 1 live inbox
    # subscriber)" / "UNKNOWN (heartbeat: beat is 5086s stale …; registry: …)".
    # ``status`` is left untouched for back-compat — this is ADDITIVE, the same
    # discipline ``inbox_reachable`` follows (observation published NEXT TO the
    # declaration, never overwriting it).
    result["liveness"] = _liveness_block(name, config, runtime_factory)
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

    # Operator mandate (lead a2a 1781e82a, 2026-06-14): surface
    # ``session_jsonl_bytes`` / ``session_jsonl_last_write`` /
    # ``heartbeat_at`` at the TOP level of the status JSON so the
    # kick-cycle can read MOVEMENT objectively without scraping
    # ``heartbeat.json`` or walking the SDK ``sdk_session`` block.
    # All three keys are always present; missing-data renders as
    # ``0`` / ``""`` (explicit empty values, NOT null) so consumers
    # never need a key-existence check.
    # stx-allow: fallback (reason: a state-dir read failure should never break
    # the status command — degrade to the explicit empty shape)
    try:
        from ._session_movement import resolve_state_dir, status_movement_fields

        state_dir = resolve_state_dir(name)
        movement = status_movement_fields(state_dir)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        movement = {
            "session_jsonl_bytes": 0,
            "session_jsonl_last_write": "",
            "heartbeat_at": "",
        }
    for k, v in movement.items():
        # Don't overwrite a field that a prior enrich step already set —
        # the additive contract says NEW keys, not "always replaces".
        result.setdefault(k, v)

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
    config = load_config(entry["config"], profile=entry.get("profile"))
    runtime = runtime_factory(config)
    return runtime.logs(config, lines)
