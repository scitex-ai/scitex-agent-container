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

    Three methods:
      * ``sdk-alive`` (default) — ask THE CONFIG'S runtime whether its
        process is up. The name is historical: it resolves through
        ``_get_runtime``, so a ``tui`` spec is asked via
        ``TuiSessionRuntime`` and an SDK spec via ``ClaudeSessionRuntime``.
        For a ``tui`` agent this ALSO gates on the turn bridge — see below.
      * ``a2a-card`` — probe the A2A AgentCard endpoint (higher
        fidelity, confirms the HTTP surface is actually serving).
      * ``turn-bridge`` — probe the TUI turn bridge's ``/health`` alone.

    THE TURN-BRIDGE GATE (2026-08-11 incident). ``sdk-alive`` asks only "is
    the tmux session alive?", so on the night 14 of 15 host-side turn bridges
    were dead PIDs, every one of those agents reported GREEN while every
    pushed ``/v1/turn`` wake was refused. Health could not see the fault, and
    ``a2a-card`` — the only HTTP-fidelity option — was no help even when
    opted into: it probes
    ``/agents/<name>/.well-known/agent-card.json``, a route the TUI bridge
    does NOT serve (``_tui_turn_bridge`` answers ``GET /health`` and 404s
    everything else), so it fails against a perfectly healthy bridge. So the
    DEFAULT path now additionally requires the bridge to answer on the route
    it really serves. A dead bridge must not report green.

    The gate applies ONLY to ``runtime: tui`` agents that actually have a
    resolved a2a port — an agent with the sidecar disabled has no bridge to
    miss, and a non-TUI agent serves ``/v1/turn`` from its own SDK runner.

    Parameters
    ----------
    runtime:
        Optional injected runtime (real collaborator). Used by
        ``sdk-alive``. Default ``None`` resolves the config's runtime via
        ``_get_runtime`` lazily.
    """
    method = config.health.method or "sdk-alive"
    if method == "sdk-alive":
        alive, message = _check_sdk_alive(config, runtime=runtime)
        if not alive:
            # A dead runtime dominates: reporting "bridge down" for an agent
            # whose session is gone would name a symptom instead of the cause.
            return alive, message
        return _gate_on_turn_bridge(config, message)
    if method == "a2a-card":
        return _check_a2a_card(config)
    if method == "turn-bridge":
        return _check_turn_bridge(config)
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


def _is_tui_runtime(config: AgentConfig) -> bool:
    """True iff this spec runs under the TUI runtime (the default when unset).

    Mirrors ``_runtime_select._get_runtime``'s mapping — ``""`` and ``"tui"``
    both mean TuiSessionRuntime — so the gate covers exactly the agents that
    HAVE a host-side turn bridge.
    """
    return (getattr(config, "runtime", "") or "tui").strip() in ("", "tui")


def _gate_on_turn_bridge(config: AgentConfig, healthy_message: str) -> tuple[bool, str]:
    """Require a live turn bridge before an ALREADY-alive TUI agent reads green.

    Returns ``healthy_message`` unchanged for anything with no bridge to miss
    (a non-TUI runtime, or a TUI agent whose a2a sidecar is disabled /
    unclaimed), so this can never invent a failure for an agent that never had
    the endpoint in the first place.
    """
    if not _is_tui_runtime(config):
        return True, healthy_message
    from ._tui_bridge_supervisor import resolve_bridge_port

    if resolve_bridge_port(config) is None:
        return True, healthy_message
    bridge_ok, bridge_message = _check_turn_bridge(config)
    if not bridge_ok:
        return False, bridge_message
    return True, f"{healthy_message}; {bridge_message}"


# How many times a FAILING bridge probe is repeated before the verdict stands,
# and how long between attempts. See ``_check_turn_bridge`` for why a single
# observation is not enough.
_BRIDGE_PROBE_ATTEMPTS = 3
_BRIDGE_PROBE_BACKOFF_S = 0.5


def _probe_turn_bridge_once(
    *, url: str, port: int, agent_name: str, remediation: str
) -> tuple[bool, str, bool]:
    """One GET of the bridge's ``/health``. Returns ``(ok, message, retryable)``.

    ``retryable`` marks the verdicts a SECOND look could legitimately overturn
    — a refused connection, a timeout, a transient non-200. An identity
    mismatch is deliberately NOT retryable: a foreign process holding the port
    is a settled fact, and re-asking only delays a report that will not change.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode())
            raw = resp.read()
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — a non-200 from the bridge is a health verdict, not a crash)
        return (
            False,
            f"unhealthy: turn bridge HTTP {exc.code} from {url} — {remediation}",
            True,
        )
    except (
        urllib.error.URLError,
        OSError,
    ) as exc:  # stx-allow: fallback (reason: connection refused / timeout IS the fault being detected — report it as a verdict, do not raise)
        return (
            False,
            f"unhealthy: turn bridge unreachable at {url} ({exc}) — {remediation}",
            True,
        )
    if status != 200:
        return (
            False,
            f"unhealthy: turn bridge HTTP {status} from {url} — {remediation}",
            True,
        )
    try:
        payload = json.loads(raw or b"")
    except (
        ValueError,
        json.JSONDecodeError,
    ) as exc:  # stx-allow: fallback (reason: a non-JSON 200 means something that is NOT our bridge holds the port — a health verdict, not a crash)
        return (
            False,
            f"unhealthy: {url} returned 200 but not our bridge's JSON ({exc}) — "
            f"something else is holding port {port}",
            False,
        )
    served = payload.get("agent") if isinstance(payload, dict) else None
    if served != agent_name:
        return (
            False,
            f"unhealthy: {url} is served by agent {served!r}, not {agent_name!r} "
            f"— a FOREIGN process holds this agent's turn-bridge port {port}",
            False,
        )
    return True, "", False


def _check_turn_bridge(
    config: AgentConfig,
    *,
    attempts: int = _BRIDGE_PROBE_ATTEMPTS,
    backoff_s: float = _BRIDGE_PROBE_BACKOFF_S,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Probe the TUI turn bridge's ``GET /health`` — the route it really serves.

    The bridge answers ``200 {"status": "ok", "agent": "<name>"}`` on
    ``/health`` and 404s every other GET (``_tui_turn_bridge._TurnBridgeHandler
    .do_GET``), so this is the ONLY GET that can distinguish "serving" from
    "dead" — which is why the pre-existing ``a2a-card`` probe (which asks for
    ``/agents/<name>/.well-known/agent-card.json``) could not be reused: it
    fails against a perfectly healthy bridge.

    Address resolution goes through the SAME helpers the launcher uses
    (``resolved_a2a_host`` + ``_tui_bridge_supervisor.resolve_bridge_port``,
    which falls back to the allocator's claim because fleet specs declare
    ``a2a.port: auto``), so health probes exactly where the bridge binds rather
    than where the raw YAML appears to point.

    CORROBORATED, NOT SINGLE-SHOT. A failing probe is repeated ``attempts``
    times ``backoff_s`` apart and the agent is only failed when EVERY attempt
    fails; any success returns healthy immediately. This is the same discipline
    ``_agents_restart_login_expired`` already applies ("READ-ONLY +
    2-run-corroborated"), and it is not optional here for a reason of our own
    making: the heartbeat supervisor RESPAWNS a dead bridge, and a respawn has
    a genuine sub-second window where the old process is gone and the new one
    has not bound yet. A single-shot probe landing in that window would report
    a self-healing system as broken. The cost is bounded and paid only by
    already-failing agents — a truly dead bridge refuses instantly, so the
    whole corroboration costs ~1s of sleeps, while a healthy bridge answers on
    the first attempt and never sleeps at all.

    The response IDENTITY is checked, not just the status code: a foreign
    process holding this agent's port would answer 200 for something else, and
    calling that healthy would be the same blindness in a new place.
    """
    from ..runtimes._tui_turn_bridge_lifecycle import resolved_a2a_host
    from ._tui_bridge_supervisor import resolve_bridge_port

    port = resolve_bridge_port(config)
    if port is None:
        return False, (
            f"unhealthy: agent {config.name!r} has no resolved a2a port, so its "
            "turn bridge cannot be probed (spec.a2a disabled, or no port claim "
            "— has the agent been started?)"
        )
    host = resolved_a2a_host(config)
    url = f"http://{host}:{port}/health"
    remediation = (
        f"the host-side turn bridge for {config.name!r} is NOT serving {url}, so "
        "every pushed /v1/turn wake from its container is refused and the agent "
        "cannot be driven by a message. `sac listen`'s TUI heartbeat tick "
        "re-asserts it within ~30s; if this persists, read "
        f"<runtime>/{config.name}/tui-turn-bridge.log for why it exits"
    )
    total = max(1, int(attempts))
    t0 = time.time()
    message = ""
    for attempt in range(1, total + 1):
        ok, message, retryable = _probe_turn_bridge_once(
            url=url, port=port, agent_name=config.name, remediation=remediation
        )
        if ok:
            elapsed_ms = int((time.time() - t0) * 1000)
            return True, f"turn bridge healthy ({elapsed_ms} ms via {host}:{port})"
        if not retryable:
            return False, message
        if attempt < total:
            sleep_fn(backoff_s)
    return False, f"{message} [confirmed by {total} probes {backoff_s}s apart]"


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
