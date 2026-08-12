"""Re-assert the TUI ``/v1/turn`` turn bridge on the existing heartbeat tick.

THE INCIDENT (scitex-compute-04, 2026-08-11). ``runtime: tui`` agents serve
``/v1/turn`` from a HOST-SIDE process (``python -m
scitex_agent_container.runtimes._tui_turn_bridge``) that
:func:`..runtimes._tui_turn_bridge_lifecycle.start_turn_bridge` spawns exactly
ONCE, from ``TuiSessionRuntime.start`` via ``_maybe_start_turn_bridge``.
NOTHING supervised it. When it died it stayed dead — and the agent still
reported healthy. Measured that night: 15 ``tui-turn-bridge.pid`` files on the
host, 14 pointing at dead PIDs; two of those agents (``canary-resume-test``
port 19014, ``scitex-live-paper`` port 19015) had LIVE tmux sessions with
NOTHING bound to their port, so their in-container channel subscriber POSTed
wake turns into a closed socket — 40 attempts, 40 connection-refused, zero
successes, a flat line at zero rather than a flap — while the operator saw 34
"unavailable: connection refused" messages and replies limped in ~354 s late
over an unrelated rail.

Two defects, one fix:

  D1 — NO SUPERVISION. Start path only, and every layer swallows failure:
       ``_tui_bridge_seam`` catches and logs a warning, ``start_turn_bridge``
       catches a spawn failure and returns ``None``. No systemd unit, no timer.
  D2 — BOOT-ORDER RACE. ``_maybe_start_turn_bridge`` runs LAST in ``start()``
       — after the container is up, after the boot drain, after the
       startup-prompt injection — while the container's subscriber is ALREADY
       POSTing. Measured 47 s and 8 operator-visible warnings on one boot.

This module closes both WITHOUT a new daemon, a systemd unit or a timer. The
centralized :func:`._tui_heartbeat_loop.tui_heartbeat_loop` already ticks every
~30 s over exactly the right population (every ``runtime: tui`` agent) and
already holds a fresh batched tmux snapshot naming which of them are ALIVE. On
each tick we now additionally ask, per live agent, "is anything bound to the
port this agent's bridge is supposed to serve?" and re-spawn when nothing is.
The first tick after a boot repairs D2 as a side effect, bounding the
connection-refused window at one tick instead of "forever".

WHY THE HEARTBEAT TICK IS THE RIGHT HOME: it is the one loop that already
enumerates TUI agents WITH their config, already runs on the host (where the
bridge process lives), already runs off the event loop under a bounded budget,
and already carries an overlap guard. A second supervisor would duplicate all
four and add a lifecycle of its own to supervise — the exact regress this fixes.

SAFETY — this only ever re-asserts, never stops. A deliberately-stopped agent
has no tmux session, so it is absent from the snapshot and never touched; only
an agent sac believes is RUNNING can have its bridge respawned.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Kill switch, mirroring SAC_TUI_HEARTBEAT_DISABLED / SAC_SDK_HEARTBEAT_DISABLED.
DISABLE_ENV = "SAC_TUI_BRIDGE_SUPERVISION_DISABLED"

# tmux session-name prefix the heartbeat snapshot is keyed by (``tui-<name>``),
# the same convention ``_tui_heartbeat_loop._beat_one`` looks agents up under.
_SESSION_PREFIX = "tui-"

# Pre-spawn port-free wait for a SUPERVISOR respawn, deliberately much shorter
# than ``start_turn_bridge``'s 10 s default. We only ever call the launcher
# after OBSERVING the port free, so the gate should confirm-and-go; the long
# default exists for the restart path, where an old holder is still shutting
# down. Keeping it short bounds the tick even if a foreign process grabs the
# port in the microseconds between our probe and the launcher's.
_RESPAWN_PORT_FREE_TIMEOUT_S = 2.0

# Per-agent outcomes. Returned (not just logged) so the tick — and tests — can
# assert what the supervisor actually decided for each agent.
VERDICT_NO_SESSION = "no-session"
VERDICT_NO_CONFIG = "no-config"
VERDICT_NO_PORT = "no-port"
VERDICT_SERVING = "serving"
VERDICT_RESTARTED = "restarted"
VERDICT_FAILED = "restart-failed"


def _default_port_lookup(name: str) -> int | None:
    """Production ``port_lookup_fn``: this agent's claimed a2a port, or None."""
    from .._state import port_allocator

    return port_allocator.get_port(name)


def resolve_bridge_port(
    config: Any,
    *,
    port_lookup_fn: Callable[[str], int | None] | None = None,
) -> int | None:
    """Return the port THIS agent's turn bridge is supposed to serve, else None.

    Two sources, tried in the order the system itself resolves them:

    1. A concrete int already on ``config.a2a.port`` — an operator PIN, or a
       config object ``_a2a_port.resolve_a2a_port`` has already mutated. Read
       via :func:`..runtimes._tui_turn_bridge_lifecycle.resolved_a2a_port`,
       the SAME reader ``start_turn_bridge`` consults, so the supervisor and
       the launcher agree about the port by construction rather than by luck.

    2. The allocator's CLAIM for this agent name. This branch is what makes the
       supervisor work at all: fleet specs declare ``a2a.port: auto`` (measured
       on compute-04 — every TUI spec), which ``load_config`` hands back
       verbatim as the string ``"auto"``. A freshly-loaded config therefore
       knows NO port, so a spec-only reader would return None for the entire
       fleet and this supervisor would be a silent no-op. ``sac agents start``
       resolved that ``auto`` through ``_state.port_allocator.claim_port``, and
       the claim is the durable record of the answer — it is what ``sac
       listen`` routes on and what was threaded into the in-container
       subscriber's ``--turn-url``. Verified against reality: the claim for
       ``scitex-agent-container`` is 19016, which is both the ``--port`` of its
       live bridge process and the URL its CCT poller POSTs to.

    READ-ONLY on purpose: we look a claim UP, we never ``claim_port``. An agent
    with no claim has no port anyone is POSTing to, so there is nothing to
    supervise; inventing one would bind a port the subscriber never learned
    about and burn a claim for an agent that is not even running.

    ``a2a.port: None`` means the sidecar is DELIBERATELY disabled and must stay
    that way, so it short-circuits BEFORE the claim lookup. The spec reader
    alone cannot tell "disabled" from "auto" — both are simply "not an int" —
    which is why that case is checked here explicitly.
    """
    from ..runtimes._tui_turn_bridge_lifecycle import resolved_a2a_port

    pinned = resolved_a2a_port(config)
    if pinned is not None:
        return pinned
    a2a = getattr(config, "a2a", None)
    if a2a is None or getattr(a2a, "port", None) is None:
        return None  # no a2a block, or sidecar explicitly disabled
    lookup = port_lookup_fn if port_lookup_fn is not None else _default_port_lookup
    claimed = lookup(str(getattr(config, "name", "") or ""))
    if isinstance(claimed, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(claimed, int) and claimed > 0:
        return claimed
    return None


def bridge_is_serving(
    host: str, port: int, *, port_free_fn: Callable[[str, int], bool]
) -> bool:
    """True iff SOMETHING holds ``host:port`` — i.e. a bridge is up there.

    Reuses the launcher's OWN
    :func:`..runtimes._tui_turn_bridge_port.port_is_free` bind probe (the same
    ``SO_REUSEADDR`` options ``_TurnBridgeServer`` sets), so "free" here means
    exactly "the next ``start_turn_bridge`` would bind here". The supervisor
    and the launcher can therefore never disagree about whether the port is
    available, which is what keeps this from respawning into an EADDRINUSE.

    A BIND PROBE, NOT AN HTTP PROBE, deliberately. It opens no connection,
    cannot be delayed by a busy handler thread, and answers precisely the
    question the respawn decision depends on. Whether a BOUND bridge is
    actually *answering* is the HEALTH layer's question, and
    :func:`.health._check_turn_bridge` asks it over real HTTP — the two probes
    are complementary, and a bound-but-wedged or FOREIGN holder shows up red
    there rather than being papered over here (re-spawning onto a held port
    would only fail the launcher's own gate).
    """
    return not port_free_fn(host, port)


def _pin_port(config: Any, port: int) -> None:
    """Write the resolved ``port`` onto ``config.a2a`` for the launcher.

    ``start_turn_bridge`` re-reads the port from the config it is handed, so a
    config still carrying the literal ``"auto"`` would make it return None and
    the respawn would silently do nothing. This is the same in-place mutation
    ``_a2a_port.resolve_a2a_port`` performs at start; the config object is the
    tick's own freshly-loaded copy, so nothing else observes it.
    """
    a2a = getattr(config, "a2a", None)
    if a2a is not None:
        a2a.port = port


def _supervise_one(
    agent: dict,
    *,
    name: str,
    snapshot: dict,
    port_free_fn: Callable[[str, int], bool],
    start_fn: Callable[..., Any],
    host_fn: Callable[[Any], str],
    port_lookup_fn: Callable[[str], int | None] | None,
) -> str:
    """Re-assert one agent's bridge. Returns a ``VERDICT_*``. Never raises."""
    if snapshot.get(f"{_SESSION_PREFIX}{name}") is None:
        # No live tmux session: the agent is stopped/absent, so it must NOT
        # have a bridge. Never resurrect one for a deliberately-stopped agent.
        return VERDICT_NO_SESSION
    config = agent.get("config")
    if config is None:
        return VERDICT_NO_CONFIG
    try:
        port = resolve_bridge_port(config, port_lookup_fn=port_lookup_fn)
        if port is None:
            return VERDICT_NO_PORT
        host = host_fn(config)
        if bridge_is_serving(host, port, port_free_fn=port_free_fn):
            return VERDICT_SERVING
    except Exception as exc:  # stx-allow: fallback (reason: per-agent best-effort — one agent's unreadable config / claim-DB hiccup must not abort supervision for the rest of the fleet; logged, skipped)
        logger.warning(
            "tui-bridge-supervisor: could not evaluate the bridge for %r "
            "(skipped this tick): %s",
            name,
            exc,
        )
        return VERDICT_NO_PORT
    # NOTHING IS BOUND but the agent is LIVE — this is the fault. Loud, because
    # a silent self-heal would hide a crash-looping bridge behind a healthy
    # port: every re-assertion is one death that went otherwise unexplained.
    logger.warning(
        "tui-bridge-supervisor: agent %r is LIVE but NOTHING is bound to its "
        "turn-bridge port %s:%d — /v1/turn wake POSTs from its container are "
        "being refused. Re-asserting the bridge now. (See "
        "<runtime>/%s/tui-turn-bridge.log for why the previous one exited.)",
        name,
        host,
        port,
        name,
    )
    _pin_port(config, port)
    try:
        pid = start_fn(config, port_free_timeout_s=_RESPAWN_PORT_FREE_TIMEOUT_S)
    except Exception as exc:  # stx-allow: fallback (reason: a respawn failure — e.g. a foreign process grabbing the port between probe and spawn — must not abort the tick for the other agents; surfaced at ERROR and retried next tick)
        logger.error(
            "tui-bridge-supervisor: FAILED to re-assert the turn bridge for %r "
            "on %s:%d — this agent cannot be woken by a pushed message until "
            "this succeeds. Retrying next tick. Cause: %s",
            name,
            host,
            port,
            exc,
        )
        return VERDICT_FAILED
    if pid is None:
        logger.error(
            "tui-bridge-supervisor: re-assert for %r on %s:%d produced NO pid "
            "(the launcher declined to spawn — missing config_path, or the "
            "port went unresolvable). Retrying next tick.",
            name,
            host,
            port,
        )
        return VERDICT_FAILED
    logger.warning(
        "tui-bridge-supervisor: re-asserted the turn bridge for %r on %s:%d "
        "(pid=%s); /v1/turn is served again.",
        name,
        host,
        port,
        pid,
    )
    return VERDICT_RESTARTED


def supervise_bridges(
    agents: list,
    *,
    snapshot: dict,
    port_free_fn: Callable[[str, int], bool] | None = None,
    start_fn: Callable[..., Any] | None = None,
    host_fn: Callable[[Any], str] | None = None,
    port_lookup_fn: Callable[[str], int | None] | None = None,
) -> dict:
    """Re-assert the turn bridge for every LIVE TUI agent whose port is unbound.

    ``agents`` are the records :func:`._tui_heartbeat_loop.list_tui_agents`
    yields (``name`` / ``state_dir`` / ``config``); ``snapshot`` is the SAME
    batched ``{session_name: activity_epoch}`` map the tick already fetched, so
    supervision costs ZERO additional tmux calls — the only per-agent work is a
    non-blocking bind probe.

    Returns ``{agent_name: VERDICT_*}``. Never raises: every per-agent failure
    is logged and converted to a verdict, so one bad agent cannot cost the rest
    of the fleet its supervision.

    Production defaults are bound when a seam is ``None``: ``port_free_fn`` →
    ``runtimes._tui_turn_bridge_port.port_is_free``; ``start_fn`` →
    ``runtimes._tui_turn_bridge_lifecycle.start_turn_bridge``; ``host_fn`` →
    ``resolved_a2a_host``; ``port_lookup_fn`` →
    ``_state.port_allocator.get_port``.
    """
    if os.environ.get(DISABLE_ENV, "") == "1":
        return {}
    if port_free_fn is None:
        from ..runtimes._tui_turn_bridge_port import port_is_free as port_free_fn
    if start_fn is None:
        from ..runtimes._tui_turn_bridge_lifecycle import (
            start_turn_bridge as start_fn,
        )
    if host_fn is None:
        from ..runtimes._tui_turn_bridge_lifecycle import (
            resolved_a2a_host as host_fn,
        )

    verdicts: dict = {}
    for agent in list(agents):
        if not isinstance(agent, dict):
            continue
        name = str(agent.get("name") or "")
        if not name:
            continue
        verdicts[name] = _supervise_one(
            agent,
            name=name,
            snapshot=snapshot,
            port_free_fn=port_free_fn,
            start_fn=start_fn,
            host_fn=host_fn,
            port_lookup_fn=port_lookup_fn,
        )
    return verdicts


__all__ = [
    "DISABLE_ENV",
    "VERDICT_FAILED",
    "VERDICT_NO_CONFIG",
    "VERDICT_NO_PORT",
    "VERDICT_NO_SESSION",
    "VERDICT_RESTARTED",
    "VERDICT_SERVING",
    "bridge_is_serving",
    "resolve_bridge_port",
    "supervise_bridges",
]
