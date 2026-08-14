"""SDK / claude-session heartbeat writer — liveness parity for the
non-TUI runtimes.

The TUI runtime has a centralized ``tui_heartbeat_loop`` that keeps a
quiet-but-live ``runtime: tui`` agent's ``heartbeat.json`` fresh (so it
reads "running" and shows a moving ``heartbeat_at``). The headless
``runtime: claude-agent-sdk`` runner writes its OWN heartbeat from
INSIDE the container — but only while its conversation loop is actively
ticking; if that in-container loop is not observable from the host (a
detached / reparented runner, or a beat that lands on the container's
own tmpfs rather than the host ``/state`` bind), the host-side
``heartbeat_at`` freezes at the last start time even though the agent is
provably alive.

This module closes that gap the SAME way ``tui_heartbeat_loop`` does for
TUI agents: a CENTRALIZED async loop in the listen server that, every
tick,

  1. lists agents whose resolved ``spec.runtime`` selects a non-TUI
     (SDK / claude-session) runtime;
  2. for each one whose DECLARED runtime reports ``is_running`` True
     (the same probe ``sac agents status`` / ``sac agents list`` use —
     :func:`_lifecycle._runtime_select._get_runtime`);
  3. writes a FRESH ``heartbeat.json`` beat via the existing
     :func:`_session_state.write_heartbeat`, stamping ``ts`` with the
     current wall clock (a live agent's host-side liveness signal), so
     ``heartbeat_at`` moves for a running SDK agent that isn't itself
     landing host-visible beats.

Sibling of :func:`_tui_heartbeat_loop.tui_heartbeat_loop`: same
create-task → tick → honour-cancellation contract, same per-agent /
per-tick best-effort resilience, same injection seams so tests drive the
loop deterministically without a real runtime / registry / state-dir IO.

SCALING + THE UNKNOWN-IS-NOT-DEAD RULE (shared with the TUI loop): the
per-agent probes run CONCURRENTLY on a bounded pool, each individually
bounded, so one wedged probe degrades to UNKNOWN for that agent instead
of holding the tick until it blows its budget and gets ABANDONED. An
abandoned tick wrote NO beats at all, which let live agents go stale and
read as "stopped" — and ``agent_send`` then refused to deliver to them.
This loop only ever WRITES fresh beats: it never records a "dead" verdict
and never erases an existing beat, so a failed probe or a dropped tick
leaves the last-known-good heartbeat intact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from pathlib import Path
from typing import Any, Callable

from ..config._harness_registry import host_probed_runtime_spellings

logger = logging.getLogger(__name__)

# Default cadence. The SDK ``health.interval`` default is 300s; beating
# every ~30s keeps ``heartbeat_at`` comfortably fresh inside that window.
# Override via ``SAC_SDK_HEARTBEAT_INTERVAL_S`` at the wiring site.
DEFAULT_SDK_HEARTBEAT_INTERVAL_S = 30.0

# Probe fan-out. Mirrors ``get_agent_list_data``'s bounded pool: enough
# concurrency that dozens of agents finish in a fraction of the tick
# budget, small enough to stay well clear of the process/fd walls a wide
# fan-out hits on a loaded host.
DEFAULT_MAX_PARALLEL_PROBES = 8

# Per-probe ceiling. A healthy SDK liveness probe is a pidfile read plus
# ``os.kill(pid, 0)`` — sub-millisecond. This only fires on a genuinely
# stalled FS read, and yields UNKNOWN (no beat), never "dead".
DEFAULT_PROBE_TIMEOUT_S = 2.0

# Runtimes handled by the TUI loop, NOT here (avoid double-beating a TUI
# agent — the TUI loop stamps the pane-activity epoch, this loop would
# clobber it with wall clock). DERIVED from the harness registry (v4
# step 4): the spellings of every entry whose ``beat_writer`` is
# ``host-probe`` — exactly the agents whose beats another loop owns.
_TUI_RUNTIMES = host_probed_runtime_spellings()

# State written for a live SDK agent. ``running`` mirrors the
# observability vocabulary the TUI/SDK heartbeat use; the read side
# (``_session_movement.heartbeat_iso``) only consumes ``ts``.
_SDK_HEARTBEAT_STATE = "running"


def list_sdk_agents() -> list[dict]:
    """Production default for the ``agent_lister`` seam.

    Returns one record per agent whose resolved ``spec.runtime`` selects
    a NON-TUI runtime (``claude-agent-sdk`` and the back-compat
    ``apptainer`` alias), each carrying:

      * ``name``      — the agent / spec directory name.
      * ``config``    — the loaded :class:`AgentConfig` (the liveness
        probe needs it to select the declared runtime).
      * ``state_dir`` — the resolved on-disk state dir (``Path``), or
        ``None`` when neither the project-scope nor home-scope candidate
        exists yet. The loop skips ``None`` (nothing to write into).

    Sources mirror :func:`_tui_heartbeat_loop.list_tui_agents`: the
    runtime Registry first, then specs defined on disk that are not
    registered. A spec that fails to load contributes nothing
    (best-effort). De-duped by name (registry wins).
    """
    from ..config import load_config
    from ._session_movement import resolve_state_dir

    out: list[dict] = []
    seen: set[str] = set()

    def _consider(name: str, config_path: str | None) -> None:
        if not name or name in seen:
            return
        if not config_path:
            return
        try:
            cfg = load_config(config_path)
        except Exception:  # stx-allow: fallback (one bad spec contributes nothing — best-effort enumeration)
            return
        runtime = getattr(cfg, "runtime", None) or ""
        if runtime in _TUI_RUNTIMES:
            return  # TUI agents are handled by tui_heartbeat_loop
        seen.add(name)
        state_dir = resolve_state_dir(name)
        out.append({"name": name, "config": cfg, "state_dir": state_dir})

    try:
        from .._state.registry import Registry

        for row in Registry().list_all():
            if not isinstance(row, dict):
                continue
            _consider(row.get("name", ""), row.get("config"))
    except Exception as exc:  # stx-allow: fallback (registry read failure must not blank the on-disk specs below)
        logger.debug("list_sdk_agents: registry enumeration failed: %s", exc)

    try:
        from ..cli_pkg._helpers._agent_list import _discover_defined_agents

        for name, spec_path in _discover_defined_agents():
            _consider(name, str(spec_path))
    except Exception as exc:  # stx-allow: fallback (on-disk discovery is additive; registry rows already captured)
        logger.debug("list_sdk_agents: on-disk spec discovery failed: %s", exc)

    return out


def _beat_one(
    agent: dict,
    *,
    is_running_fn: Callable[[Any], bool | None],
    write_fn: Callable[..., None],
    now_fn: Callable[[], float] = time.time,
) -> bool:
    """Write one SDK agent's heartbeat if its declared runtime reports it
    running. Best-effort — never raises.

    Returns ``True`` iff a heartbeat was written (runtime reported the
    agent alive AND a state dir exists), ``False`` otherwise. A per-agent
    failure is logged at debug and swallowed so one bad agent cannot
    abort the tick.
    """
    name = agent.get("name", "")
    config = agent.get("config")
    state_dir = agent.get("state_dir")
    if not name or config is None or state_dir is None:
        return False
    try:
        if not is_running_fn(config):
            return False
        write_fn(
            Path(state_dir),
            pid=0,
            state=_SDK_HEARTBEAT_STATE,
            ts=float(now_fn()),
        )
        return True
    except Exception as exc:  # stx-allow: fallback (per-agent best-effort: one failure must not abort the tick — logged, skipped)
        logger.debug("sdk_heartbeat: beat for %r failed (skipped): %s", name, exc)
        return False


def _default_is_running(config: Any) -> bool | None:
    """Production default for the ``is_running_fn`` seam: probe the
    agent's DECLARED runtime (the same selection ``sac agents status`` /
    ``sac agents list`` use) so a running SDK agent is recognised
    identically across all three surfaces.
    """
    from ._runtime_select import _get_runtime

    return _get_runtime(config).is_running(config)


async def sdk_heartbeat_loop(
    *,
    interval_s: float = DEFAULT_SDK_HEARTBEAT_INTERVAL_S,
    agent_lister: Any = None,
    is_running_fn: Any = None,
    write_fn: Any = None,
    max_parallel_probes: int = DEFAULT_MAX_PARALLEL_PROBES,
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    tick_timeout_s: float | None = None,
) -> None:
    """Long-running SDK/claude-session heartbeat-writer task for the
    listen lifespan.

    Seams (production defaults bound when ``None``): ``agent_lister`` →
    :func:`list_sdk_agents`; ``is_running_fn`` → :func:`_default_is_running`
    (declared-runtime probe); ``write_fn`` →
    :func:`_runners._session_state.write_heartbeat`.

    Probes run CONCURRENTLY on a bounded pool (``max_parallel_probes``,
    each capped at ``probe_timeout_s``), mirroring
    :func:`cli_pkg._helpers._agent_list.get_agent_list_data`. Serially, one
    wedged probe (a stalled pidfile read on a loaded host) held the whole
    tick until it blew its budget and got ABANDONED — writing no beats at
    all, which is what let live agents go stale and read as "stopped".

    ``tick_timeout_s`` is the ``off_loop`` budget for ONE tick (default
    ``max(interval_s, 15.0)``). Exceeding it ABANDONS that tick — SAFE by
    construction: an abandoned tick writes nothing, so the previous
    heartbeats survive as last-known-good (UNKNOWN, never dead).
    """
    if os.environ.get("SAC_SDK_HEARTBEAT_DISABLED", "") == "1":
        logger.info("sdk_heartbeat_loop: disabled via SAC_SDK_HEARTBEAT_DISABLED")
        return

    # ROOT-CAUSE GUARD (mirrors tui_heartbeat_loop): the per-tick body
    # walks the registry + on-disk specs and probes each agent's runtime
    # (tmux / kill / FS reads). On the event loop a hung probe would
    # starve uvicorn's bind, so run the whole tick off the loop with a
    # hard timeout.
    from ._off_loop import run_blocking_or

    lister = agent_lister if agent_lister is not None else list_sdk_agents
    probe = is_running_fn if is_running_fn is not None else _default_is_running
    if write_fn is None:
        from .._runners._session_state import write_heartbeat as write_fn

    # OVERLAP GUARD — see the twin comment in ``tui_heartbeat_loop``. An
    # abandoned tick's thread is never joined, so without this the loop
    # would stack a fresh probe thread on top of every slow one, saturating
    # the SHARED default executor that agent_restart / host_exec depend on.
    tick_lock = threading.Lock()

    def _tick_body() -> None:
        if not tick_lock.acquire(blocking=False):
            logger.warning(
                "sdk_heartbeat_loop: previous tick still in flight — SKIPPING "
                "this one (not stacking a second probe thread). Liveness data "
                "from the last good tick is retained (UNKNOWN, not dead)."
            )
            return
        try:
            agents = list(lister())
            if not agents:
                return
            # Probe every agent CONCURRENTLY, each bounded. A probe that
            # times out yields UNKNOWN — we simply write no beat for it,
            # leaving its previous heartbeat as last-known-good. We never
            # write a "dead" verdict, so a slow probe can never flip a live
            # agent to dead.
            pool = ThreadPoolExecutor(max_workers=max(1, int(max_parallel_probes)))
            try:
                future_to_agent = {
                    pool.submit(probe, agent.get("config")): agent for agent in agents
                }
                for future, agent in future_to_agent.items():
                    try:
                        alive = future.result(timeout=probe_timeout_s)
                    except _FuturesTimeout:  # stx-allow: fallback (a wedged probe is UNKNOWN liveness — no beat, previous heartbeat retained; never "dead")
                        future.cancel()
                        logger.debug(
                            "sdk_heartbeat: probe for %r timed out (liveness "
                            "UNKNOWN; previous heartbeat retained)",
                            agent.get("name", ""),
                        )
                        continue
                    except Exception as exc:  # stx-allow: fallback (per-agent probe failure is UNKNOWN, not dead — logged, skipped)
                        logger.debug(
                            "sdk_heartbeat: probe for %r failed (skipped): %s",
                            agent.get("name", ""),
                            exc,
                        )
                        continue
                    if alive:
                        _beat_one(
                            agent,
                            is_running_fn=lambda _cfg: True,
                            write_fn=write_fn,
                        )
            finally:
                # shutdown(wait=False): never join a wedged probe thread —
                # that would defeat the per-probe timeout above.
                pool.shutdown(wait=False)
        finally:
            tick_lock.release()

    budget_s = (
        float(tick_timeout_s) if tick_timeout_s is not None else max(interval_s, 15.0)
    )
    logger.info(
        "sdk_heartbeat_loop: starting (interval_s=%.1f tick_timeout_s=%.1f)",
        interval_s,
        budget_s,
    )
    try:
        while True:
            try:
                await run_blocking_or(
                    _tick_body,
                    default=None,
                    op="sdk_heartbeat_loop tick (runtime probes)",
                    timeout_s=budget_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must survive a transient registry/runtime/FS error; logged, retried next tick)
                logger.warning(
                    "sdk_heartbeat_loop: tick failed (%s); retry next tick", exc
                )
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("sdk_heartbeat_loop: cancelled cleanly")
        raise


__all__ = [
    "DEFAULT_MAX_PARALLEL_PROBES",
    "DEFAULT_PROBE_TIMEOUT_S",
    "DEFAULT_SDK_HEARTBEAT_INTERVAL_S",
    "list_sdk_agents",
    "sdk_heartbeat_loop",
]
