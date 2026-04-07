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

    if method == "screen-alive":
        return _check_screen_alive(config.screen_name)

    return False, f"Unknown health method: {method}"


def _check_screen_alive(screen_name: str) -> tuple[bool, str]:
    """Check if a screen session exists."""
    result = subprocess.run(
        ["screen", "-ls", screen_name],
        capture_output=True,
        text=True,
    )
    if screen_name in result.stdout:
        return True, "healthy"
    return False, "unhealthy: screen session not found"


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
