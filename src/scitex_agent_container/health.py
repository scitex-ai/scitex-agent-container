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
    if method == "a2a-card":
        return _check_a2a_card(config)

    return False, f"Unknown health method: {method}"


def _check_a2a_card(config: AgentConfig) -> tuple[bool, str]:
    """Probe the agent's A2A AgentCard endpoint.

    Reads ``spec.a2a.{port,host}`` from the YAML and issues a GET to
    ``http://<host>:<port>/v1/agents/<name>/.well-known/agent.json``.
    Healthy iff the endpoint returns 200 with ``name == config.name``.

    Used when ``spec.health.method: a2a-card`` is set in v3 YAML —
    higher-fidelity than ``multiplexer-alive`` because it confirms
    the agent's A2A surface is actually serving, not just that the
    multiplexer session exists.
    """
    import json
    import urllib.error
    import urllib.request

    from .runtimes.a2a_sidecar import _read_a2a_block

    a2a = _read_a2a_block(config)
    if a2a is None:
        return False, "unhealthy: spec.a2a not set in YAML"

    host = str(a2a.get("host", "127.0.0.1"))
    port = int(a2a["port"])
    url = f"http://{host}:{port}/v1/agents/{config.name}/.well-known/agent.json"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        return False, f"unhealthy: AgentCard HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError) as exc:  # stx-allow: fallback (reason: file system operation failure)
        return False, f"unhealthy: AgentCard unreachable at {url}: {exc}"
    except (ValueError, json.JSONDecodeError) as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
        return False, f"unhealthy: AgentCard malformed JSON: {exc}"

    elapsed_ms = int((time.time() - t0) * 1000)
    if not isinstance(data, dict) or data.get("name") != config.name:
        return False, (
            f"unhealthy: AgentCard name mismatch "
            f"(expected {config.name!r}, got {data.get('name')!r})"
        )
    return True, f"healthy ({elapsed_ms} ms via {host}:{port})"


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
                except Exception:  # stx-allow: fallback (reason: non-fatal restart failure — health monitor loop must continue regardless)
                    traceback.print_exc()

            retries += 1
            current_backoff = min(current_backoff * backoff_multiplier, backoff_max)
