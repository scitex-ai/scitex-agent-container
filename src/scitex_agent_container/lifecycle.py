"""Agent lifecycle management -- start, stop, restart, status."""

from __future__ import annotations

import subprocess
import threading
import time
import traceback
from pathlib import Path

from .config import AgentConfig, load_config, resolve_config
from .health import health_monitor
from .registry import Registry
from .runtimes.claude_code import ClaudeCodeRuntime


def _get_runtime(config: AgentConfig):
    """Return the appropriate runtime for the config."""
    if config.runtime == "claude-code":
        return ClaudeCodeRuntime()
    raise ValueError(f"Unsupported runtime: {config.runtime}")


def _run_hooks(hooks: list[str], extra_env: dict[str, str] | None = None) -> None:
    """Execute a list of shell hook commands.

    Args:
        hooks: Shell commands to execute.
        extra_env: Additional env vars passed to hook subprocesses
            (e.g., SCITEX_AGENT_CONTAINER_CONFIG_PATH, SCITEX_AGENT_CONTAINER_SCREEN_NAME, SCITEX_AGENT_CONTAINER_NAME).
    """
    import os

    env = {**os.environ, **(extra_env or {})}
    for hook in hooks:
        if not hook:
            continue
        result = subprocess.run(
            hook, shell=True, capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            # Log but don't fail
            import sys

            print(f"[WARN] Hook failed: {hook}", file=sys.stderr)
            if result.stderr:
                print(f"       {result.stderr.strip()}", file=sys.stderr)


def agent_start(
    config_path: str,
    registry: Registry | None = None,
    no_preflight: bool = False,
    force: bool = False,
) -> bool:
    """Start an agent from a YAML config file.

    Args:
        config_path: Path to the YAML agent definition.
        registry: Optional registry instance.
        no_preflight: If True, skip SSH preflight checks (useful for slow hosts).
        force: If True and the agent is already running, stop it first
            and then start fresh. Also tolerates stale registry entries
            and ghost screens (via force-stop).

    Returns True on success, False on failure.
    """
    config_path = resolve_config(config_path)
    registry = registry or Registry()
    config = load_config(config_path)
    runtime = _get_runtime(config)

    # Already running?
    if registry.exists(config.name) and runtime.is_running(config):
        if not force:
            raise RuntimeError(f"Agent '{config.name}' is already running")
        agent_stop(config.name, registry=registry, force=True)
        # Small grace period so the screen is fully torn down before we
        # try to create a new one with the same name.
        time.sleep(1)
    elif force and registry.exists(config.name):
        # Registry says it exists but runtime says not running — stale entry.
        agent_stop(config.name, registry=registry, force=True)

    # Hook env vars — let hooks know about the agent context
    hook_env = {
        "SCITEX_AGENT_CONTAINER_CONFIG_PATH": str(Path(config_path).resolve()),
        "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
        "SCITEX_AGENT_CONTAINER_NAME": config.name,
    }

    # Pre-start hooks
    _run_hooks(config.hooks.get("pre_start", []), extra_env=hook_env)

    # Start — pass ``force`` through so remote dispatchers (SSHRemote)
    # can relay ``--force`` to the remote CLI and skip its own
    # already-running check.
    # Config-level no_preflight overrides CLI flag
    if config.remote.no_preflight:
        no_preflight = True
    success = runtime.start(config, no_preflight=no_preflight, force=force)
    if not success:
        raise RuntimeError(f"Failed to start agent '{config.name}'")

    # Register
    registry.add(
        name=config.name,
        config_path=str(Path(config_path).resolve()),
        screen_name=config.screen_name,
    )

    # Post-start hooks
    _run_hooks(config.hooks.get("post_start", []), extra_env=hook_env)

    # Start health monitor in background if enabled
    if config.health.enabled:
        thread = threading.Thread(
            target=health_monitor,
            args=(config.name, config, registry, lambda c: _get_runtime(c).start(c)),
            daemon=True,
        )
        thread.start()

    return True


def agent_stop(
    name: str,
    registry: Registry | None = None,
    force: bool = False,
) -> bool:
    """Stop a running agent by name.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        force: If True, do not fail when the agent is missing from the
            registry or when hooks/runtime.stop() raise; wipe stale
            state and return True. Useful for bulk cleanup.
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        if force:
            return True
        raise RuntimeError(f"Agent '{name}' not found in registry")

    try:
        config = load_config(entry["config"])
    except Exception:
        if not force:
            raise
        # Config gone — just nuke the registry entry
        registry.remove(name)
        return True

    runtime = _get_runtime(config)

    hook_env = {
        "SCITEX_AGENT_CONTAINER_CONFIG_PATH": str(Path(entry["config"]).resolve()),
        "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
        "SCITEX_AGENT_CONTAINER_NAME": config.name,
    }

    # Pre-stop hooks
    try:
        _run_hooks(config.hooks.get("pre_stop", []), extra_env=hook_env)
    except Exception:
        if not force:
            raise

    try:
        runtime.stop(config)
    except Exception:
        if not force:
            raise

    # Post-stop hooks
    try:
        _run_hooks(config.hooks.get("post_stop", []), extra_env=hook_env)
    except Exception:
        if not force:
            raise

    registry.remove(name)
    return True


def agent_stop_all(
    registry: Registry | None = None,
    force: bool = False,
) -> list[tuple[str, bool, str]]:
    """Stop every agent in the registry.

    Returns a list of ``(name, success, message)`` tuples, one per agent.
    With ``force=True``, continues through errors so a partial failure
    doesn't block cleanup of the rest.
    """
    registry = registry or Registry()
    results: list[tuple[str, bool, str]] = []
    for entry in registry.list_all():
        name = entry.get("name", "?")
        try:
            agent_stop(name, registry=registry, force=force)
            results.append((name, True, "stopped"))
        except Exception as exc:
            results.append((name, False, str(exc)))
            if not force:
                break
    return results


def agent_restart(name: str, registry: Registry | None = None) -> bool:
    """Restart an agent by name."""
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    config_path = entry["config"]
    agent_stop(name, registry)
    time.sleep(2)
    return agent_start(config_path, registry)


def agent_status(name: str, registry: Registry | None = None) -> dict:
    """Get detailed status for an agent."""
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    try:
        config = load_config(entry["config"])
        runtime = _get_runtime(config)
        running = runtime.is_running(config)
    except Exception:
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
    if config and config.remote.is_remote:
        result["remote"] = config.remote.host
    return result


def agent_logs(name: str, lines: int = 50, registry: Registry | None = None) -> str:
    """Get recent logs from an agent."""
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    config = load_config(entry["config"])
    runtime = _get_runtime(config)
    return runtime.logs(config, lines)
