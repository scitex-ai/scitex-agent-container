"""Health check implementation for running agents."""

from __future__ import annotations

import time
import traceback
from typing import Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig


def health_check(
    config: AgentConfig,
    *,
    runtime: object | None = None,
) -> tuple[bool, str]:
    """Run a single health check. Returns (is_healthy, message).

    Two methods:
      * ``sdk-alive`` (default) — ask THE CONFIG'S runtime whether its
        process is up. The name is historical: it resolves through
        ``_get_runtime``, so a ``tui`` spec is asked via
        ``TuiSessionRuntime`` and an SDK spec via ``ClaudeSessionRuntime``.
      * ``a2a-card`` — probe the A2A AgentCard endpoint (higher
        fidelity, confirms the HTTP surface is actually serving).

    Parameters
    ----------
    runtime:
        Optional injected runtime (real collaborator). Used by
        ``sdk-alive``. Default ``None`` resolves the config's runtime via
        ``_get_runtime`` lazily.
    """
    method = config.health.method or "sdk-alive"
    if method == "sdk-alive":
        return _check_sdk_alive(config, runtime=runtime)
    if method == "a2a-card":
        return _check_a2a_card(config)
    return False, f"Unknown health method: {method}"


def _check_sdk_alive(
    config: AgentConfig,
    *,
    runtime: object | None = None,
) -> tuple[bool, str]:
    """Ask THE CONFIG'S runtime whether its process is up.

    ``runtime`` is an injectable real collaborator. Default ``None``
    resolves via :func:`._runtime_select._get_runtime` — the canonical
    selector every other lifecycle path already uses.

    IT USED TO HARDCODE ``ClaudeSessionRuntime``, and that made this check
    UNPASSABLE for most of the fleet. ``ClaudeSessionRuntime.is_running``
    delegates to ``_container_runtime_for``, which resolves only
    ``apptainer`` / ``claude-agent-sdk`` and returns ``None`` for anything
    else; ``is_running`` then maps that "no runtime to ask" onto ``False``
    — unknown reported as dead. Since ``spec.runtime`` DEFAULTS to ``tui``
    (``config/_types.py``), the default configuration reported
    ``unhealthy: SDK runner not running`` while being demonstrably alive,
    and ``status_cmds`` gates ``sys.exit(1)`` on it — so ``sac agents
    health`` failed for every TUI agent, permanently. A check that cannot
    pass, which is the mirror of the checks that cannot fail.

    Measured on the host for a live ``runtime: tui`` agent before/after:

        ClaudeSessionRuntime().is_running(cfg)        -> False   (the bug)
        ApptainerContainerRuntime().is_running(cfg)   -> False   (naive fix,
                                                        ALSO wrong: a tui
                                                        agent is `apptainer
                                                        exec` in a tmux pane,
                                                        not a named instance)
        TuiSessionRuntime().is_running(cfg)           -> True    (correct)

    ``_get_runtime`` returns the third one for ``tui``/unset and the SDK
    runtime for the explicit SDK values, so routing through it fixes the
    default case without changing behaviour for SDK specs.
    """
    if runtime is None:
        from ._runtime_select import _get_runtime

        runtime = _get_runtime(config)
    if runtime.is_running(config):
        return True, "healthy"
    # Name WHICH runtime said no. The old text said "SDK runner" regardless
    # of the runtime actually consulted, which sent readers looking for an
    # SDK process that a tui agent never had.
    kind = (getattr(config, "runtime", "") or "tui").strip() or "tui"
    return False, f"unhealthy: {kind} runtime reports its process not running"


def _check_a2a_card(config: AgentConfig) -> tuple[bool, str]:
    """Probe the agent's A2A AgentCard endpoint.

    Reads ``spec.a2a.{port,host}`` from the YAML and issues a GET to
    ``http://<host>:<port>/agents/<name>/.well-known/agent-card.json``.
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
    url = f"http://{host}:{port}/agents/{config.name}/.well-known/agent-card.json"
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
    *,
    health_check_fn: Optional[Callable[[AgentConfig], tuple[bool, str]]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Background health monitor loop with restart support.

    This runs indefinitely, checking health at the configured interval
    and optionally restarting the agent on failure.

    Args:
        name: Agent name.
        config: Parsed agent config.
        registry: Registry instance for checking if agent was removed.
        restart_fn: Callable(config) -> bool to restart the agent.
        health_check_fn: Injectable health-check callable. Default
            ``None`` uses the module-level :func:`health_check` (real
            collaborator). Tests pass a real callable that returns
            scripted ``(bool, str)`` tuples.
        sleep_fn: Injectable sleep (real callable; default ``time.sleep``).
    """
    interval = config.health.interval
    policy = config.restart.policy
    max_retries = config.restart.max_retries
    backoff_initial = config.restart.backoff_initial
    backoff_max = config.restart.backoff_max
    backoff_multiplier = config.restart.backoff_multiplier

    check = health_check_fn or health_check

    retries = 0
    current_backoff = backoff_initial

    while True:
        sleep_fn(interval)

        # Stop monitoring if agent was removed from registry
        if not registry.exists(name):
            return

        is_healthy, message = check(config)
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

            sleep_fn(current_backoff)

            if restart_fn is not None:
                # stx-allow: fallback (reason: restart callback failure must not abort the health-monitor loop; error is printed and monitoring continues)
                try:
                    restart_fn(config)
                except Exception:  # stx-allow: fallback (reason: non-fatal restart failure — health monitor loop must continue regardless)
                    traceback.print_exc()

            retries += 1
            current_backoff = min(current_backoff * backoff_multiplier, backoff_max)
