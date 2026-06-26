"""TUI heartbeat writer — first-class liveness parity for ``runtime: tui``.

Operator mandate ("heartbeat must be available in tui as well"): the
status / observability layer (``agent_status``, ``sac agents list``,
the board) reads ``heartbeat.json``, which ONLY the SDK runner writes.
A live TUI agent therefore showed empty ``heartbeat_at`` and status
``stopped``/gray even while its tmux session was demonstrably alive —
``TuiSessionRuntime.is_running`` already sees it (tmux session exists
AND pane-activity within the max-idle window), but that signal never
reached ``heartbeat.json``.

This module closes that gap with a CENTRALIZED writer in the listen
server — one async loop, no per-agent sidecar lifecycle to supervise.
Every tick it:

  1. lists agents whose ``spec.runtime == "tui"`` (the ``agent_lister``
     seam; production default walks the Registry + on-disk specs);
  2. for each RUNNING one — its ``tui-<name>`` tmux session exists —
     reads the session's pane-activity epoch
     (``TmuxManager.session_activity``);
  3. writes that agent's ``heartbeat.json`` via the existing
     :func:`_session_state.write_heartbeat`, stamping ``ts`` with the
     pane-activity epoch (the SAME liveness signal ``is_running`` keys
     off) and ``state="running"``.

Sibling of :func:`_github_ci_poll_loop.github_ci_poll_loop`: same
create-task → sleep-at-boundary → honour-cancellation contract so a
``sac listen`` SIGTERM doesn't leak the loop.

Resilience (operator: fail-loud, fail-fast, no silent fallbacks):

  * **Per-agent best-effort** — one agent's failure (unresolvable state
    dir, a tmux probe hiccup) is logged at debug and skipped; it must
    NOT abort the whole tick (the other live TUI agents still get a
    fresh beat).
  * **Per-tick resilience** — a tick-level exception is logged + retried
    next tick, never fatal.
  * **Fail-loud on missing tooling** — if ``tmux`` is not installed at
    all, the loop logs ONE error and disables itself rather than
    emitting a silent stream of no-ops forever (mirrors the CI poller's
    ``gh``-missing preflight).

Every collaborator is an injection seam so tests drive the full loop
deterministically without tmux / a real registry / state-dir IO.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default cadence. The TUI-alive max-idle window is 300s
# (``runtimes.tui_session._DEFAULT_MAX_IDLE_S``); beating every ~30s
# keeps ``heartbeat_at`` comfortably fresh inside that window while
# costing a handful of cheap ``tmux`` probes per minute. Override via
# ``SAC_TUI_HEARTBEAT_INTERVAL_S`` at the wiring site.
DEFAULT_TUI_HEARTBEAT_INTERVAL_S = 30.0

# State written for a live TUI agent. ``running`` mirrors the
# observability vocabulary the SDK heartbeat uses; the read side
# (``_session_movement.heartbeat_iso``) only consumes ``ts``, but the
# field keeps the payload shape consistent for any state-aware consumer.
_TUI_HEARTBEAT_STATE = "running"


def _tmux_available() -> bool:
    """True iff the ``tmux`` binary is on ``$PATH`` (fail-loud preflight)."""
    return shutil.which("tmux") is not None


def list_tui_agents() -> list[dict]:
    """Production default for the ``agent_lister`` seam.

    Returns one record per agent whose resolved ``spec.runtime`` is
    ``"tui"``, each carrying:

      * ``name``      — the agent / spec directory name.
      * ``state_dir`` — the resolved on-disk state dir (``Path``), or
        ``None`` when neither the project-scope nor home-scope candidate
        exists yet. The loop skips ``None`` (nothing to write into).

    No ``pid`` is carried: a TUI agent's real process lives inside the
    container under tmux and is managed via ``tmux kill-session``, not a
    host-pid signal (the registry only records the ephemeral, already-dead
    ``sac agents start`` launcher pid). The heartbeat read side keys off
    ``ts``, so the loop writes ``pid=0`` rather than a misleading value.

    Sources mirror :func:`cli_pkg._helpers._agent_list.get_agent_list_data`:
    the runtime Registry first, then specs defined on disk that are not
    registered. A spec that fails to load contributes nothing (best-effort
    — one bad YAML never blocks the rest). De-duped by name (registry
    wins, matching the list command's precedence).
    """
    from .._state.registry import Registry
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
        if getattr(cfg, "runtime", None) != "tui":
            return
        seen.add(name)
        state_dir = resolve_state_dir(name)
        if state_dir is None:
            # Fall back to the runtime's own resolver: a just-started TUI
            # agent always has its state dir (materialize_workspace made
            # it), even if the read-side resolver's is-dir probe lost a
            # race. Best-effort — None stays None if even this fails.
            try:
                from ..runtimes.tui_session import state_dir_for_config

                state_dir = state_dir_for_config(cfg)
            except Exception:  # stx-allow: fallback (resolver import/build may fail in a partial install)
                state_dir = None
        out.append({"name": name, "state_dir": state_dir})

    try:
        for row in Registry().list_all():
            if not isinstance(row, dict):
                continue
            _consider(row.get("name", ""), row.get("config"))
    except Exception as exc:  # stx-allow: fallback (registry read failure must not blank the on-disk specs below)
        logger.debug("list_tui_agents: registry enumeration failed: %s", exc)

    # Specs defined on disk but not (yet) in the registry — same source
    # the list command merges in so an on-disk TUI agent is not missed.
    try:
        from ..cli_pkg._helpers._agent_list import _discover_defined_agents

        for name, spec_path in _discover_defined_agents():
            _consider(name, str(spec_path))
    except Exception as exc:  # stx-allow: fallback (on-disk discovery is additive; registry rows already captured)
        logger.debug("list_tui_agents: on-disk spec discovery failed: %s", exc)

    return out


def _beat_one(
    agent: dict,
    *,
    session_exists_fn: Callable[[str], bool],
    activity_fn: Callable[[str], int | None],
    write_fn: Callable[..., None],
) -> bool:
    """Write one TUI agent's heartbeat if its session is live. Best-effort.

    Returns ``True`` iff a heartbeat was written (session exists AND a
    pane-activity epoch was available), ``False`` otherwise. Never raises
    — a per-agent failure is logged at debug and swallowed so one bad
    agent cannot abort the tick.
    """
    name = agent.get("name", "")
    state_dir = agent.get("state_dir")
    if not name or state_dir is None:
        return False
    session_name = f"tui-{name}"
    try:
        if not session_exists_fn(session_name):
            return False
        activity = activity_fn(session_name)
        if activity is None:
            # Session exists but no readable activity stamp — not a live,
            # responsive TUI (matches is_running's None→False contract).
            return False
        write_fn(
            Path(state_dir),
            pid=0,
            state=_TUI_HEARTBEAT_STATE,
            ts=float(activity),
        )
        return True
    except Exception as exc:  # stx-allow: fallback (per-agent best-effort: one failure must not abort the tick — logged, skipped)
        logger.debug("tui_heartbeat: beat for %r failed (skipped): %s", name, exc)
        return False


async def tui_heartbeat_loop(
    *,
    interval_s: float = DEFAULT_TUI_HEARTBEAT_INTERVAL_S,
    agent_lister: Any = None,
    session_exists_fn: Any = None,
    activity_fn: Any = None,
    write_fn: Any = None,
    tmux_check: Any = None,
) -> None:
    """Long-running TUI heartbeat-writer task for the listen lifespan.

    Seams (production defaults bound when ``None``): ``agent_lister`` →
    :func:`list_tui_agents`; ``session_exists_fn`` →
    :meth:`_runners._tmux.tmux.TmuxManager.exists`; ``activity_fn`` →
    :meth:`TmuxManager.session_activity`; ``write_fn`` →
    :func:`_runners._session_state.write_heartbeat`; ``tmux_check`` →
    :func:`_tmux_available`.
    """
    if os.environ.get("SAC_TUI_HEARTBEAT_DISABLED", "") == "1":
        logger.info("tui_heartbeat_loop: disabled via SAC_TUI_HEARTBEAT_DISABLED")
        return

    # ROOT-CAUSE GUARD (cards sac-listen-self-peer-persist-blocks-bind /
    # sac-listen-watchdog-autorestart-alarm): like the CI poller, this
    # preflight and the per-tick body run BEFORE/around the first
    # ``await`` and shell out to ``tmux`` (blocking subprocess.run) plus a
    # registry walk. On the event loop, a hung ``tmux`` here starves
    # uvicorn's bind. Run every blocking step off the loop with a hard
    # timeout so a wedged probe degrades that step instead of the daemon.
    from ._off_loop import run_blocking_or

    check = tmux_check if tmux_check is not None else _tmux_available
    if not await run_blocking_or(check, default=False, op="tmux preflight (which tmux)"):
        logger.error(
            "tui_heartbeat_loop: `tmux` is not installed — TUI heartbeat "
            "writing DISABLED. TUI agents will show empty heartbeat_at on "
            "this host until tmux is on $PATH. (fail-loud: refusing to run "
            "a writer that can observe nothing)"
        )
        return

    lister = agent_lister if agent_lister is not None else list_tui_agents
    if session_exists_fn is None or activity_fn is None:
        from .._runners._tmux.tmux import TmuxManager

        if session_exists_fn is None:
            session_exists_fn = TmuxManager.exists
        if activity_fn is None:
            activity_fn = TmuxManager.session_activity
    if write_fn is None:
        from .._runners._session_state import write_heartbeat as write_fn

    def _tick_body() -> None:
        # ``lister()`` walks the registry + on-disk specs and each
        # ``_beat_one`` probes ``tmux`` (blocking subprocess.run); run the
        # whole tick off the event loop so a slow/hung probe never starves
        # the listen server even after bind.
        for agent in list(lister()):
            _beat_one(
                agent,
                session_exists_fn=session_exists_fn,
                activity_fn=activity_fn,
                write_fn=write_fn,
            )

    logger.info("tui_heartbeat_loop: starting (interval_s=%.1f)", interval_s)
    try:
        while True:
            try:
                await run_blocking_or(
                    _tick_body,
                    default=None,
                    op="tui_heartbeat_loop tick (tmux probes)",
                    timeout_s=max(interval_s, 15.0),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must survive a transient registry/tmux/FS error; logged, retried next tick)
                logger.warning(
                    "tui_heartbeat_loop: tick failed (%s); retry next tick", exc
                )
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("tui_heartbeat_loop: cancelled cleanly")
        raise


__all__ = [
    "DEFAULT_TUI_HEARTBEAT_INTERVAL_S",
    "list_tui_agents",
    "tui_heartbeat_loop",
]
