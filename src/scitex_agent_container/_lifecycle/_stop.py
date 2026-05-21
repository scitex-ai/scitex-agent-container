"""``agent_stop`` / ``agent_stop_all`` / ``agent_restart``.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports all three names.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ._a2a_port import release_a2a_port
from ._handover_loader import _load_handover_module
from ._hook_runner import _fire_forget_hook, _run_hooks
from ._instances import end_local_instance as _end_local_instance
from ._runtime_select import _get_runtime


def agent_stop(
    name: str,
    registry: Registry | None = None,
    force: bool = False,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    handover_mod: Any = None,
) -> bool:
    """Stop a running agent by name.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        force: If True, do not fail when the agent is missing from the
            registry or when hooks/runtime.stop() raise; wipe stale
            state and return True. Useful for bulk cleanup.
        runtime_factory: Injectable real runtime factory (default
            :func:`_get_runtime`).
        handover_mod: Injectable real handover collaborator (default
            ``None`` resolves to the real
            :mod:`._lifecycle.handover` module).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        if force:
            return True
        raise RuntimeError(f"Agent '{name}' not found in registry")

    # stx-allow: fallback (reason: YAML file may have been deleted while the agent was registered; force-stop must succeed even without a config)
    try:
        config = load_config(entry["config"])
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
        # Config gone — just nuke the registry entry
        registry.remove(name)
        return True

    runtime_factory = runtime_factory or _get_runtime
    runtime = runtime_factory(config)

    hook_env = {
        "SCITEX_AGENT_CONTAINER_CONFIG_PATH": str(Path(entry["config"]).resolve()),
        "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
        "SCITEX_AGENT_CONTAINER_NAME": config.name,
    }

    # ZOO#12 FR-A — push a sentinel snapshot to the hub right before
    # the agent stops, so a future agent_start (here or on a different
    # host) can hydrate. Best-effort: never block the stop path on a
    # hub outage. The sentinel is a marker; the agent's own pre_stop
    # hook is the right place for richer state (transcript, memory).
    try:
        _h = handover_mod if handover_mod is not None else _load_handover_module()
        _h.push_pre_stop_snapshot(config)
    except Exception:
        traceback.print_exc()

    # Pre-stop hooks
    # stx-allow: fallback (reason: hook commands may reference paths or env vars absent at stop time; force-stop must continue regardless)
    try:
        _run_hooks(config.hooks.get("pre_stop", []), extra_env=hook_env)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
    _fire_forget_hook(config.name, "pre_stop", config.hooks.get("pre_stop", []))

    # stx-allow: fallback (reason: tmux/screen session may already be dead; force-stop should still proceed to clean up registry)
    try:
        runtime.stop(config)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise

    # Post-stop hooks
    # stx-allow: fallback (reason: post-stop hooks are best-effort notification; a failed hook must not prevent registry cleanup)
    try:
        _run_hooks(config.hooks.get("post_stop", []), extra_env=hook_env)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
    _fire_forget_hook(config.name, "post_stop", config.hooks.get("post_stop", []))

    # Mark the local state.db ``instances`` row ended so subsequent
    # ``send_to_agent`` calls correctly report "not running" and the
    # unique (name, host, scope) active-row index is freed for a restart.
    _end_local_instance(config, runtime)

    # Release the A2A port claim so the next agent can re-use it.
    release_a2a_port(name)
    registry.remove(name)
    return True


def agent_stop_all(
    registry: Registry | None = None,
    force: bool = False,
    *,
    stop_fn: Optional[Callable[..., bool]] = None,
) -> list[tuple[str, bool, str]]:
    """Stop every agent in the registry.

    Returns a list of ``(name, success, message)`` tuples, one per agent.
    With ``force=True``, continues through errors so a partial failure
    doesn't block cleanup of the rest.

    Args:
        registry: Optional registry instance.
        force: Continue through individual-agent failures.
        stop_fn: Injectable real per-agent stop callable (default
            ``None`` uses module-level :func:`agent_stop`). Tests pass a
            real callable that records calls and optionally raises.
    """
    registry = registry or Registry()
    stopper = stop_fn or agent_stop
    results: list[tuple[str, bool, str]] = []
    for entry in registry.list_all():
        name = entry.get("name", "?")
        # stx-allow: fallback (reason: stopping one agent may fail due to a missing config or dead session; other agents in the registry should still be stopped)
        try:
            stopper(name, registry=registry, force=force)
            results.append((name, True, "stopped"))
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            results.append((name, False, str(exc)))
            if not force:
                break
    return results


def agent_restart(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    handover_mod: Any = None,
) -> bool:
    """Restart an agent by name.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
        sleep_fn: Real sleep (default ``time.sleep``).
        handover_mod: Real handover collaborator (default ``None``
            resolves to the real module).
    """
    # Lazy import breaks the ``_start`` <-> ``_stop`` cycle.
    from ._start import agent_start

    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    config_path = entry["config"]
    agent_stop(
        name,
        registry,
        runtime_factory=runtime_factory,
        handover_mod=handover_mod,
    )
    sleep_fn(2)
    return agent_start(
        config_path,
        registry,
        runtime_factory=runtime_factory,
        sleep_fn=sleep_fn,
        handover_mod=handover_mod,
    )
