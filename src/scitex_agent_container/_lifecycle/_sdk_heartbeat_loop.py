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
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default cadence. The SDK ``health.interval`` default is 300s; beating
# every ~30s keeps ``heartbeat_at`` comfortably fresh inside that window.
# Override via ``SAC_SDK_HEARTBEAT_INTERVAL_S`` at the wiring site.
DEFAULT_SDK_HEARTBEAT_INTERVAL_S = 30.0

# Runtimes handled by the TUI loop, NOT here (avoid double-beating a TUI
# agent — the TUI loop stamps the pane-activity epoch, this loop would
# clobber it with wall clock).
_TUI_RUNTIMES = frozenset({"", "tui"})

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
) -> None:
    """Long-running SDK/claude-session heartbeat-writer task for the
    listen lifespan.

    Seams (production defaults bound when ``None``): ``agent_lister`` →
    :func:`list_sdk_agents`; ``is_running_fn`` → :func:`_default_is_running`
    (declared-runtime probe); ``write_fn`` →
    :func:`_runners._session_state.write_heartbeat`.
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

    def _tick_body() -> None:
        for agent in list(lister()):
            _beat_one(agent, is_running_fn=probe, write_fn=write_fn)

    logger.info("sdk_heartbeat_loop: starting (interval_s=%.1f)", interval_s)
    try:
        while True:
            try:
                await run_blocking_or(
                    _tick_body,
                    default=None,
                    op="sdk_heartbeat_loop tick (runtime probes)",
                    timeout_s=max(interval_s, 15.0),
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
    "DEFAULT_SDK_HEARTBEAT_INTERVAL_S",
    "list_sdk_agents",
    "sdk_heartbeat_loop",
]
