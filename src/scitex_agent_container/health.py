"""Health check implementation for running agents."""

from __future__ import annotations

import subprocess
import time
import traceback

from .config import AgentConfig
from .registry import Registry


def health_check(config: AgentConfig) -> tuple[bool, str]:
    """Run a single health check. Returns (is_healthy, message)."""
    method = config.health.method

    if method == "multiplexer-alive":
        if config.remote.is_remote:
            return _check_session_alive_remote(config)
        return _check_session_alive(config)

    return False, f"Unknown health method: {method}"


def _check_session_alive(config: AgentConfig) -> tuple[bool, str]:
    """Check if a multiplexer session exists locally."""
    from .runtimes.multiplexer import get_multiplexer

    mux = get_multiplexer(config)
    if mux.exists(config.screen_name):
        return True, "healthy"
    return False, f"unhealthy: {config.multiplexer} session not found"


def _check_session_alive_remote(config: AgentConfig) -> tuple[bool, str]:
    """Check if a multiplexer session exists on remote machine."""
    session_name = config.screen_name or f"cld-{config.name}"
    # Determine the check command based on multiplexer type
    if config.multiplexer == "tmux":
        check_cmd = (
            f"tmux has-session -t {session_name} 2>/dev/null && echo {session_name}"
        )
    else:
        check_cmd = f"screen -ls {session_name}"
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if config.remote.key:
        ssh_cmd += ["-i", config.remote.key]
    if config.remote.port != 22:
        ssh_cmd += ["-p", str(config.remote.port)]
    target = (
        f"{config.remote.user}@{config.remote.host}"
        if config.remote.user
        else config.remote.host
    )
    ssh_cmd += [target, check_cmd]
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
    if session_name in result.stdout:
        return True, f"healthy (remote: {config.remote.host})"
    return (
        False,
        f"unhealthy: {config.multiplexer} session not found on {config.remote.host}",
    )


def health_monitor(
    name: str,
    config: AgentConfig,
    registry: Registry,
    restart_fn=None,
) -> None:
    """Background health monitor loop with restart support.

    This runs indefinitely, checking health at the configured interval
    and optionally restarting the agent on failure.

    Args:
        name: Agent name.
        config: Parsed agent config.
        registry: Registry instance for checking if agent was removed.
        restart_fn: Callable(config) -> bool to restart the agent.
    """
    interval = config.health.interval
    policy = config.restart.policy
    max_retries = config.restart.max_retries
    backoff_initial = config.restart.backoff_initial
    backoff_max = config.restart.backoff_max
    backoff_multiplier = config.restart.backoff_multiplier

    retries = 0
    current_backoff = backoff_initial

    while True:
        time.sleep(interval)

        # Stop monitoring if agent was removed from registry
        if not registry.exists(name):
            return

        is_healthy, message = health_check(config)
        if is_healthy:
            retries = 0
            current_backoff = backoff_initial
            continue

        # Unhealthy
        if policy == "never":
            continue

        if policy in ("on-failure", "always"):
            if retries >= max_retries:
                return

            time.sleep(current_backoff)

            if restart_fn is not None:
                try:
                    restart_fn(config)
                except Exception:
                    traceback.print_exc()

            retries += 1
            current_backoff = min(current_backoff * backoff_multiplier, backoff_max)
