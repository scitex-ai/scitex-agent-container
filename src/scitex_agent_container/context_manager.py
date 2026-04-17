"""Context-lifecycle management for running agents.

Polls the tmux pane of an agent, parses the claude-hud statusline
percentage, and triggers a compact/restart action when usage crosses
the configured threshold. See todo#284 / todo#285.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig, ContextManagementConfig

logger = logging.getLogger(__name__)

# Match patterns like " 73%" or "ctx 73%" or "(42%)" near end of line.
# Accepts optional decimals and is whitespace/bracket tolerant.
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9.])(\d{1,3}(?:\.\d+)?)\s*%")


def parse_context_percent(pane_text: str) -> float | None:
    """Return the claude-hud context-usage percent from a pane capture.

    Strategy: scan from the bottom of the buffer upward (that's where the
    statusline lives) and return the first plausible percentage we find.
    Returns ``None`` if nothing matches; callers treat that as "no signal
    this tick" and skip dispatch.
    """
    if not pane_text:
        return None
    for line in reversed(pane_text.splitlines()):
        for match in _PERCENT_RE.finditer(line):
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if 0.0 <= value <= 100.0:
                return value
    return None


# 2026-04-17 runtime/ layout: client scripts live under shared/scripts/.
_DEFAULT_AGENT_META_SCRIPT = "~/.scitex/orochi/shared/scripts/agent_meta.py"


def fetch_agent_meta(
    agent_name: str, script_path: str | None = None
) -> dict[str, Any] | None:
    """Shell out to ``agent_meta.py <agent>`` and return its JSON dict.

    Returns ``None`` on ANY failure (missing script, bad JSON, non-zero
    exit, timeout). Never raises. The resolved script path can be
    overridden via the ``SCITEX_AGENT_META_SCRIPT`` env var or the
    ``script_path`` argument (argument wins).
    """
    explicit = script_path or os.environ.get("SCITEX_AGENT_META_SCRIPT")
    path = explicit if explicit else _DEFAULT_AGENT_META_SCRIPT
    resolved = str(Path(path).expanduser())
    try:
        r = subprocess.run(
            [resolved, agent_name],
            timeout=5,
            capture_output=True,
            text=True,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None
    try:
        data = (
            json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        )
    except (json.JSONDecodeError, IndexError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# Default pane-capture fn. Injectable for tests; production uses tmux.
def _default_capture(session_name: str) -> str:
    from .runtimes.tmux import TmuxManager

    return TmuxManager.capture_content(session_name)


Dispatcher = Callable[[str, AgentConfig], None]
Capturer = Callable[[str], str]


# Module-level registry of live sensors keyed by agent name. Mirrors the
# pattern used by health_monitor; consumers (e.g. ``agent_status``) read
# this to surface the most recent context-usage percent in status --json.
_SENSORS: dict[str, "ContextManager"] = {}


def get_sensor(agent_name: str) -> "ContextManager | None":
    """Return the live sensor for ``agent_name`` if one is running."""
    return _SENSORS.get(agent_name)


class ContextManager:
    """Sensor + strategy dispatcher for a single agent."""

    def __init__(
        self,
        agent_name: str,
        session_name: str,
        config: ContextManagementConfig,
        dispatcher: Dispatcher,
        agent_config: AgentConfig | None = None,
        capture: Capturer | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.session_name = session_name
        self.config = config
        self.dispatcher = dispatcher
        self.agent_config = agent_config
        self.capture = capture or _default_capture
        self._stop = threading.Event()
        self._fired = False  # latch — don't re-dispatch until we observe a drop
        self._ticks_near_threshold = 0
        self.last_percent: float | None = None
        self.last_meta: dict[str, Any] | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def tick(self) -> float | None:
        """One sensor probe. Returns the observed percent, or None.

        Preferred source is the Orochi ``agent_meta.py`` helper which
        reads Claude's live transcript; falls back to scraping the tmux
        pane for a claude-hud statusline when the helper is unavailable.
        """
        percent: float | None = None
        meta = fetch_agent_meta(self.agent_name)
        if meta is not None:
            self.last_meta = meta
            raw = meta.get("context_pct")
            if isinstance(raw, (int, float)):
                percent = float(raw)
        if percent is None:
            pane = self.capture(self.session_name)
            percent = parse_context_percent(pane)
        if percent is None:
            logger.debug(
                "context_manager[%s]: no percent from meta or pane", self.agent_name
            )
            return None
        self.last_percent = percent

        threshold = self.config.trigger_at_percent
        logger.debug(
            "context_manager[%s]: percent=%.1f threshold=%.1f",
            self.agent_name,
            percent,
            threshold,
        )

        # Reset the latch once usage drops well below the threshold (e.g.
        # after a successful /compact).
        if self._fired and percent < threshold - 10.0:
            self._fired = False
            self._ticks_near_threshold = 0

        if percent >= threshold:
            if self._fired:
                return percent
            self._fired = True
            logger.warning(
                "context_manager[%s]: percent %.1f >= %.1f — dispatching %s",
                self.agent_name,
                percent,
                threshold,
                self.config.strategy,
            )
            try:
                self.dispatcher(self.config.strategy, self.agent_config)
            except Exception:  # pragma: no cover — defensive
                logger.exception(
                    "context_manager[%s]: dispatcher raised", self.agent_name
                )
            return percent

        # Warn window: N probes before we'd trigger, based on a simple
        # "we're within 10% headroom" heuristic. This is intentionally cheap
        # — a proper predictor lives under todo#285.
        warn_n = self.config.warn_before_n_checks
        if warn_n > 0 and percent >= max(0.0, threshold - 10.0):
            self._ticks_near_threshold += 1
            if self._ticks_near_threshold <= warn_n:
                logger.warning(
                    "context_manager[%s]: approaching threshold "
                    "(%.1f%% / %.1f%%) — %d/%d warn ticks",
                    self.agent_name,
                    percent,
                    threshold,
                    self._ticks_near_threshold,
                    warn_n,
                )
        else:
            self._ticks_near_threshold = 0

        return percent


def run_forever(cm: ContextManager) -> None:
    """Sensor loop. Cooperatively cancellable via ``cm.stop()``.

    Piggybacks a self-snapshot tick after each context-manager tick so
    both daemons share a single thread (todo#286).
    """
    interval = max(1, int(cm.config.check_interval_seconds))
    while not cm.stopped:
        try:
            cm.tick()
        except Exception:  # pragma: no cover — defensive
            logger.exception("context_manager[%s]: tick failed", cm.agent_name)
        try:
            from .snapshot import snapshot_tick

            snapshot_tick(
                cm.agent_name,
                session=cm.session_name,
                agent_config=cm.agent_config,
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception("snapshot[%s]: piggyback tick failed", cm.agent_name)
        # Use Event.wait so stop() breaks us out promptly.
        if cm._stop.wait(interval):
            break


def _last_percent_for(agent_name: str) -> float | None:
    cm = _SENSORS.get(agent_name)
    return cm.last_percent if cm is not None else None


def _fire_hook(
    agent_config: AgentConfig,
    hook_name: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Non-blocking fire of an ``on_*`` hook. Swallows all errors."""
    try:
        from .hooks import run_hook

        commands = (agent_config.hooks or {}).get(hook_name, []) or []
        run_hook(agent_config.name, hook_name, commands, context=context)
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "context_manager[%s]: %s hook dispatch failed",
            agent_config.name,
            hook_name,
        )


def default_dispatcher(strategy: str, agent_config: AgentConfig | None) -> None:
    """Production dispatcher. Translates a strategy to a concrete action."""
    if agent_config is None:
        logger.error("context_manager: dispatcher called without agent_config")
        return

    if strategy == "compact":
        from .runtimes.tmux import TmuxManager

        session = agent_config.screen_name
        # Match the cross-agent compact protocol: Escape → 0.2s → /compact → Enter
        TmuxManager.send_keys(session, "Escape")
        time.sleep(0.2)
        TmuxManager.send_keys(session, "/compact", "Enter")
        logger.info("context_manager[%s]: sent /compact", agent_config.name)
        _fire_hook(
            agent_config,
            "on_compact",
            context={
                "percent": _last_percent_for(agent_config.name),
                "strategy": "compact",
            },
        )
        return

    if strategy == "restart":
        from .lifecycle import agent_restart

        _fire_hook(
            agent_config,
            "on_restart",
            context={
                "percent": _last_percent_for(agent_config.name),
                "strategy": "restart",
            },
        )
        try:
            agent_restart(agent_config.name)
        except Exception:
            logger.exception("context_manager[%s]: restart failed", agent_config.name)
        return

    # "noop" or unknown — nothing to do.
    logger.debug("context_manager: ignoring strategy=%s", strategy)


def start_sensor(agent_config: AgentConfig) -> ContextManager | None:
    """Spawn the sensor loop in a daemon thread if the policy is enabled.

    Returns the ContextManager instance (for tests) or ``None`` if the
    feature is disabled for this agent.
    """
    cm_cfg = agent_config.context_management
    if not cm_cfg.enabled:
        return None

    cm = ContextManager(
        agent_name=agent_config.name,
        session_name=agent_config.screen_name,
        config=cm_cfg,
        dispatcher=default_dispatcher,
        agent_config=agent_config,
    )
    thread = threading.Thread(
        target=run_forever,
        args=(cm,),
        daemon=True,
        name=f"context-manager[{agent_config.name}]",
    )
    _SENSORS[agent_config.name] = cm
    thread.start()
    try:
        from .snapshot import register_sidecar

        register_sidecar(
            agent_config.name,
            kind="thread",
            name="context_manager",
            thread=thread,
        )
        register_sidecar(
            agent_config.name,
            kind="thread",
            name="snapshot",
            thread=thread,
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "context_manager[%s]: sidecar registration failed",
            agent_config.name,
        )
    logger.info(
        "context_manager[%s]: sensor started (strategy=%s, threshold=%.1f%%, interval=%ss)",
        agent_config.name,
        cm_cfg.strategy,
        cm_cfg.trigger_at_percent,
        cm_cfg.check_interval_seconds,
    )
    return cm
