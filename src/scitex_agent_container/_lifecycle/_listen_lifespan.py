"""Starlette ``lifespan`` factory for the ``sac listen`` server.

Extracted from ``_listen.server.create_app`` (which had grown past the
module line budget) AND the focal point of the silent-bind-hang fix
(cards ``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``).

The lifespan:

  1. ``await`` the one-shot self-peer persistence (app ready);
  2. launch the three background loops — periodic-drive, GitHub-CI
     poll, TUI-heartbeat — each honouring its disable env var. Their
     startup paths now route every blocking ``gh``/``tmux``/FS call
     through ``_off_loop.run_blocking*`` so none can starve uvicorn's
     bind (the root cause of the silent fleet-comms outage);
  3. launch the fail-loud bind watchdog (``_bind_watchdog``) when a
     port is known, so an up-but-not-serving daemon can NEVER stay
     silent;
  4. ``yield`` (server serves);
  5. on teardown, cancel every launched task cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def build_listen_lifespan(*, health_watchdog_port: int | None = None):
    """Return the ``@asynccontextmanager`` lifespan for ``create_app``.

    ``health_watchdog_port`` — when set, the lifespan launches the
    fail-loud bind watchdog against ``127.0.0.1:<port>/v1/health``. The
    CLI passes the port it hands to ``uvicorn.run``; in-process tests may
    omit it (no watchdog) or pass a real bound port.
    """
    from ._bind_watchdog import bind_watchdog_loop
    from ._github_ci_poll_loop import (
        DEFAULT_CI_POLL_INTERVAL_S,
        github_ci_poll_loop,
    )
    from ._periodic_drive_loop import periodic_drive_loop
    from ._tui_heartbeat_loop import (
        DEFAULT_TUI_HEARTBEAT_INTERVAL_S,
        tui_heartbeat_loop,
    )

    # Imported lazily inside the helper below to avoid a server↔lifecycle
    # import cycle at module load.
    @asynccontextmanager
    async def _lifespan(app):  # type: ignore[no-untyped-def]
        from .._listen._self_peer_persistence import (
            persist_self_peers_on_listen_startup,
        )

        await persist_self_peers_on_listen_startup()
        tasks: list = []

        # Periodic-drive listen-loop (lead a2a 7916f486, 2026-06-14).
        # Honour SAC_PERIODIC_DRIVE_DISABLED=1 to skip launching.
        if os.environ.get("SAC_PERIODIC_DRIVE_DISABLED", "") != "1":
            task = asyncio.create_task(periodic_drive_loop(app.state))
            app.state.periodic_drive_task = task
            tasks.append(task)

        # GitHub-CI verdict-delivery poll loop (sac #404, feedback.pdf §3).
        # Self-disables (fail-loud) when `gh` is unauthenticated or
        # SAC_GITHUB_CI_POLLER_DISABLED=1, so launch unconditionally.
        # Cadence override: SAC_GITHUB_CI_POLL_INTERVAL_S.
        try:
            _ci_interval = float(
                os.environ.get(
                    "SAC_GITHUB_CI_POLL_INTERVAL_S", DEFAULT_CI_POLL_INTERVAL_S
                )
            )
        except (TypeError, ValueError):
            _ci_interval = DEFAULT_CI_POLL_INTERVAL_S
        ci_task = asyncio.create_task(github_ci_poll_loop(poll_interval_s=_ci_interval))
        app.state.github_ci_poller_task = ci_task
        tasks.append(ci_task)

        # TUI heartbeat writer (operator: "heartbeat must be available in
        # tui as well"). Self-disables (fail-loud) when `tmux` is missing
        # or SAC_TUI_HEARTBEAT_DISABLED=1, so launch unconditionally.
        # Cadence override: SAC_TUI_HEARTBEAT_INTERVAL_S.
        try:
            _tui_hb_interval = float(
                os.environ.get(
                    "SAC_TUI_HEARTBEAT_INTERVAL_S", DEFAULT_TUI_HEARTBEAT_INTERVAL_S
                )
            )
        except (TypeError, ValueError):
            _tui_hb_interval = DEFAULT_TUI_HEARTBEAT_INTERVAL_S
        tui_hb_task = asyncio.create_task(tui_heartbeat_loop(interval_s=_tui_hb_interval))
        app.state.tui_heartbeat_task = tui_hb_task
        tasks.append(tui_hb_task)

        # FAIL-LOUD bind watchdog (operator: "when failure occurs, fail
        # loud"). If we know the bind port, probe it shortly after startup
        # and scream an ERROR if the daemon is up-but-not-serving — the
        # exact silent state that took the fleet's comms down. Disable via
        # SAC_LISTEN_BIND_WATCHDOG_DISABLED=1 (e.g. odd test harnesses).
        if (
            health_watchdog_port is not None
            and os.environ.get("SAC_LISTEN_BIND_WATCHDOG_DISABLED", "") != "1"
        ):
            try:
                _wd_delay = float(os.environ.get("SAC_LISTEN_BIND_WATCHDOG_DELAY_S", ""))
            except (TypeError, ValueError):
                _wd_delay = None
            wd_kwargs = {"port": int(health_watchdog_port)}
            if _wd_delay is not None:
                wd_kwargs["delay_s"] = _wd_delay
            wd_task = asyncio.create_task(bind_watchdog_loop(**wd_kwargs))
            app.state.bind_watchdog_task = wd_task
            tasks.append(wd_task)

        try:
            yield
        finally:
            for _t in tasks:
                if _t is not None and not _t.done():
                    _t.cancel()
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):  # stx-allow: fallback (teardown best-effort; a loop's final exception must not block shutdown)
                        pass

    return _lifespan


__all__ = ["build_listen_lifespan"]
