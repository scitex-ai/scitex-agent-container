"""Starlette ``lifespan`` factory for the ``sac listen`` server.

Extracted from ``_listen.server.create_app`` (which had grown past the
module line budget) AND the focal point of the silent-bind-hang fix
(cards ``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``).

The lifespan:

  1. ``await`` the one-shot self-peer persistence (app ready);
  2. launch the background loops — periodic-drive, GitHub-CI poll,
     TUI-heartbeat, liveness-tick reconciler — each honouring its
     disable env var. Their startup paths route every blocking
     ``gh``/``tmux``/FS call through ``_off_loop.run_blocking*`` so none
     can starve uvicorn's bind (the root cause of the silent fleet-comms
     outage);
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
    from .._listen._liveness_tick import (
        DEFAULT_INTERVAL_S as DEFAULT_LIVENESS_TICK_INTERVAL_S,
        DEFAULT_RENOTIFY_S as DEFAULT_LIVENESS_TICK_RENOTIFY_S,
        DEFAULT_STALE_S as DEFAULT_LIVENESS_TICK_STALE_S,
        ENV_INTERVAL_S as LIVENESS_TICK_ENV_INTERVAL_S,
        ENV_RENOTIFY_S as LIVENESS_TICK_ENV_RENOTIFY_S,
        ENV_STALE_S as LIVENESS_TICK_ENV_STALE_S,
        liveness_tick_reconciler_loop,
    )
    from ._bind_watchdog import bind_watchdog_loop
    from ._github_ci_poll_loop import (
        DEFAULT_CI_POLL_INTERVAL_S,
        github_ci_poll_loop,
    )
    from ._periodic_drive_loop import periodic_drive_loop
    from ._sdk_heartbeat_loop import (
        DEFAULT_SDK_HEARTBEAT_INTERVAL_S,
        sdk_heartbeat_loop,
    )
    from ._tui_heartbeat_loop import (
        DEFAULT_TUI_HEARTBEAT_INTERVAL_S,
        tui_heartbeat_loop,
    )

    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    # Imported lazily inside the helper below to avoid a server↔lifecycle
    # import cycle at module load.
    @asynccontextmanager
    async def _lifespan(app):  # type: ignore[no-untyped-def]
        from .._listen._self_peer_persistence import (
            persist_self_peers_on_listen_startup,
        )

        await persist_self_peers_on_listen_startup()
        tasks: list = []

        # Best-effort ``comms_nodes`` peer-sync — launched HERE (after the
        # bind, off the event loop) rather than synchronously before
        # ``uvicorn.run``. The pre-bind synchronous sync was the live
        # silent-bind-hang vector (INCIDENT 2026-06-26): one powered-off
        # static peer made its un-timed ssh call hang, blocking boot before
        # 7878 ever bound, with no error logged. As a backgrounded task that
        # dispatches the blocking ssh sweep via ``asyncio.to_thread`` and
        # bounds it, it can never block the bind. Honour
        # SAC_LISTEN_STARTUP_SYNC_DISABLED=1 to skip launching (test
        # harnesses / single-host installs that opt out at the env level).
        if os.environ.get("SAC_LISTEN_STARTUP_SYNC_DISABLED", "") != "1":
            from .._listen._startup_peer_sync import sync_peers_on_listen_startup

            sync_task = asyncio.create_task(sync_peers_on_listen_startup())
            app.state.startup_peer_sync_task = sync_task
            tasks.append(sync_task)

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

        # SDK/claude-session heartbeat writer (fix
        # liveness-live-agents-read-stopped): parity with the TUI writer
        # for the non-TUI runtimes, so a running-but-quiet SDK agent's
        # host-side ``heartbeat_at`` stays fresh instead of freezing at
        # its start time. Self-disables via SAC_SDK_HEARTBEAT_DISABLED=1.
        # Cadence override: SAC_SDK_HEARTBEAT_INTERVAL_S.
        try:
            _sdk_hb_interval = float(
                os.environ.get(
                    "SAC_SDK_HEARTBEAT_INTERVAL_S", DEFAULT_SDK_HEARTBEAT_INTERVAL_S
                )
            )
        except (TypeError, ValueError):
            _sdk_hb_interval = DEFAULT_SDK_HEARTBEAT_INTERVAL_S
        sdk_hb_task = asyncio.create_task(
            sdk_heartbeat_loop(interval_s=_sdk_hb_interval)
        )
        app.state.sdk_heartbeat_task = sdk_hb_task
        tasks.append(sdk_hb_task)

        # Liveness-tick reconciler (card sac-card-anchored-stop-reconciler):
        # deterministic alarm-engine producer — reads cards (truth) vs.
        # real agent activity and emits an anomaly on the
        # ``scitex_todo.hooks`` bus when an OPEN, unblocked card's owner is
        # dead/idle past the stale threshold. Its FS/registry reads route
        # through ``_off_loop`` so it can never starve the bind. Honour
        # SAC_LIVENESS_TICK_DISABLED=1 to skip launching.
        if os.environ.get("SAC_LIVENESS_TICK_DISABLED", "") != "1":
            lt_task = asyncio.create_task(
                liveness_tick_reconciler_loop(
                    interval_s=_env_float(
                        LIVENESS_TICK_ENV_INTERVAL_S, DEFAULT_LIVENESS_TICK_INTERVAL_S
                    ),
                    stale_s=_env_float(
                        LIVENESS_TICK_ENV_STALE_S, DEFAULT_LIVENESS_TICK_STALE_S
                    ),
                    renotify_s=_env_float(
                        LIVENESS_TICK_ENV_RENOTIFY_S, DEFAULT_LIVENESS_TICK_RENOTIFY_S
                    ),
                )
            )
            app.state.liveness_tick_task = lt_task
            tasks.append(lt_task)

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
