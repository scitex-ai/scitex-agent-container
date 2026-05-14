"""Health check implementation for running agents."""

from __future__ import annotations

import time
import traceback

from .._state.registry import Registry
from ..config import AgentConfig


def health_check(config: AgentConfig) -> tuple[bool, str]:
    """Run a single health check. Returns (is_healthy, message).

    Two methods:
      * ``sdk-alive`` (default) — ask the SDK runtime whether its
        container/process is up.
      * ``a2a-card`` — probe the A2A AgentCard endpoint (higher
        fidelity, confirms the HTTP surface is actually serving).
    """
    method = config.health.method or "sdk-alive"
    if method == "sdk-alive":
        return _check_sdk_alive(config)
    if method == "a2a-card":
        return _check_a2a_card(config)
    return False, f"Unknown health method: {method}"


def _check_sdk_alive(config: AgentConfig) -> tuple[bool, str]:
    """Ask the SDK runtime whether the container/runner is up."""
    from ..runtimes.claude_session import ClaudeSessionRuntime

    if ClaudeSessionRuntime().is_running(config):
        return True, "healthy"
    return False, "unhealthy: SDK runner not running"


def _check_a2a_card(config: AgentConfig) -> tuple[bool, str]:
    """Probe the agent's A2A AgentCard endpoint.

    Reads ``spec.a2a.{port,host}`` from the YAML and issues a GET to
    ``http://<host>:<port>/v1/sac/agents/<name>/.well-known/agent.json``.
    Healthy iff the endpoint returns 200 with ``name == config.name``.
    """
    import json
    import urllib.error
    import urllib.request

    from ..runtimes.a2a_sidecar import _read_a2a_block

    a2a = _read_a2a_block(config)
    if a2a is None:
        return False, "unhealthy: spec.a2a not set in YAML"

    host = str(a2a.get("host", "127.0.0.1"))
    port = int(a2a["port"])
    url = f"http://{host}:{port}/v1/sac/agents/{config.name}/.well-known/agent.json"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        return False, f"unhealthy: AgentCard HTTP {exc.code} from {url}"
    except (
        urllib.error.URLError,
        OSError,
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        return False, f"unhealthy: AgentCard unreachable at {url}: {exc}"
    except (
        ValueError,
        json.JSONDecodeError,
    ) as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
        return False, f"unhealthy: AgentCard malformed JSON: {exc}"

    elapsed_ms = int((time.time() - t0) * 1000)
    if not isinstance(data, dict) or data.get("name") != config.name:
        return False, (
            f"unhealthy: AgentCard name mismatch "
            f"(expected {config.name!r}, got {data.get('name')!r})"
        )
    return True, f"healthy ({elapsed_ms} ms via {host}:{port})"


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
                # stx-allow: fallback (reason: restart callback failure must not abort the health-monitor loop; error is printed and monitoring continues)
                try:
                    restart_fn(config)
                except Exception:  # stx-allow: fallback (reason: non-fatal restart failure — health monitor loop must continue regardless)
                    traceback.print_exc()

            retries += 1
            current_backoff = min(current_backoff * backoff_multiplier, backoff_max)
