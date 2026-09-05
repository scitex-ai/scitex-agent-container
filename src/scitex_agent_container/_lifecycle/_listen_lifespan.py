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
    from .._listen._deploy_freshness import (
        DEFAULT_INTERVAL_S as DEFAULT_DEPLOY_FRESHNESS_INTERVAL_S,
        ENV_DISABLED as DEPLOY_FRESHNESS_ENV_DISABLED,
        ENV_INTERVAL_S as DEPLOY_FRESHNESS_ENV_INTERVAL_S,
        deploy_freshness_loop,
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

        # A post-bind ``comms_nodes`` peer-sync task was launched HERE until
        # 2026-08-28 (and, before PR #469, run synchronously PRE-bind, which
        # was the live silent-bind-hang vector of INCIDENT 2026-06-26: one
        # powered-off static peer made its un-timed ssh call hang, so 7878
        # never bound and the fleet lost agent-to-agent comms).
        #
        # It is GONE, not merely moved again: the ADR-0014 directory is now
        # the shared PostgreSQL store, so every host already reads the same
        # peer view and there is nothing to fetch at boot. ``sac registry
        # sync`` — the verb this task drove — was deleted in the same change.
        # ``SAC_LISTEN_STARTUP_SYNC_DISABLED`` is consequently read by
        # nothing; it is not honoured here because there is no longer
        # anything for it to disable.

        # Periodic-drive listen-loop (lead a2a 7916f486, 2026-06-14).
        # Honour SAC_PERIODIC_DRIVE_DISABLED=1 to skip launching.
        if os.environ.get("SAC_PERIODIC_DRIVE_DISABLED", "") != "1":
            task = asyncio.create_task(periodic_drive_loop(app.state))
            app.state.periodic_drive_task = task
            tasks.append(task)

        # GROUP SWITCH for the three loops immediately below — the GitHub-CI
        # poller and the two heartbeat writers, and ONLY those three. They
        # are the loops that need nothing from the host to start, so they
        # start EVERYWHERE, including in every foreign process that merely
        # boots this app to test something else.
        #
        # Each of the three also has its own `SAC_*_DISABLED` switch, and
        # those keep their published meaning. This one exists because those
        # three switches are ALSO read by the coroutines themselves, so a
        # process that sets them changes how the coroutine behaves when
        # called DIRECTLY — which is exactly what the loops' own unit tests
        # do. A caller that wants "this app, without its pollers" needs a
        # knob that the coroutines do not read. This is that knob.
        _pollers_off = os.environ.get("SAC_LISTEN_POLLER_LOOPS_DISABLED", "") == "1"

        # GitHub-CI verdict-delivery poll loop (sac #404, feedback.pdf §3).
        # Fail-loud when `gh` is unauthenticated, so a broken deploy is
        # never silent — but the KILL SWITCH is honoured HERE, at the
        # launch site, like every sibling loop above and below. It used
        # to launch unconditionally and let the coroutine self-disable,
        # which meant `SAC_GITHUB_CI_POLLER_DISABLED=1` still cost a task
        # and still LOGGED A LINE. That line is written by
        # scitex-logging's LazyStderrStreamHandler, which re-resolves
        # `sys.stderr` at every emit — so in a test process it lands in
        # whatever stream is installed right then, up to and including a
        # `CliRunner.invoke` buffer, where it corrupts a `--json`
        # assertion (card sac-clirunner-json-asserts-parse-merged-stderr).
        # Cadence override: SAC_GITHUB_CI_POLL_INTERVAL_S.
        if not _pollers_off and os.environ.get("SAC_GITHUB_CI_POLLER_DISABLED", "") != "1":
            try:
                _ci_interval = float(
                    os.environ.get(
                        "SAC_GITHUB_CI_POLL_INTERVAL_S", DEFAULT_CI_POLL_INTERVAL_S
                    )
                )
            except (TypeError, ValueError):
                _ci_interval = DEFAULT_CI_POLL_INTERVAL_S
            ci_task = asyncio.create_task(
                github_ci_poll_loop(poll_interval_s=_ci_interval)
            )
            app.state.github_ci_poller_task = ci_task
            tasks.append(ci_task)

        # TUI heartbeat writer (operator: "heartbeat must be available in
        # tui as well"). Fail-loud when `tmux` is missing; kill switch
        # honoured at the launch site for the same reason as the CI
        # poller directly above — this loop is the 30s-periodic one, so
        # it kept writing for as long as the process lived.
        # Cadence override: SAC_TUI_HEARTBEAT_INTERVAL_S.
        if not _pollers_off and os.environ.get("SAC_TUI_HEARTBEAT_DISABLED", "") != "1":
            try:
                _tui_hb_interval = float(
                    os.environ.get(
                        "SAC_TUI_HEARTBEAT_INTERVAL_S", DEFAULT_TUI_HEARTBEAT_INTERVAL_S
                    )
                )
            except (TypeError, ValueError):
                _tui_hb_interval = DEFAULT_TUI_HEARTBEAT_INTERVAL_S
            tui_hb_task = asyncio.create_task(
                tui_heartbeat_loop(interval_s=_tui_hb_interval)
            )
            app.state.tui_heartbeat_task = tui_hb_task
            tasks.append(tui_hb_task)

        # SDK/claude-session heartbeat writer (fix
        # liveness-live-agents-read-stopped): parity with the TUI writer
        # for the non-TUI runtimes, so a running-but-quiet SDK agent's
        # host-side ``heartbeat_at`` stays fresh instead of freezing at
        # its start time. Kill switch SAC_SDK_HEARTBEAT_DISABLED=1 is
        # honoured at the launch site, same as its TUI twin above.
        # Cadence override: SAC_SDK_HEARTBEAT_INTERVAL_S.
        if not _pollers_off and os.environ.get("SAC_SDK_HEARTBEAT_DISABLED", "") != "1":
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
        # ``scitex_cards.hooks`` bus when an OPEN, unblocked card's owner is
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

        # Deploy-freshness reconciler (INCIDENT 2026-07-02: a full day of
        # merged PRs ran stale because the host checkout silently sat 18
        # commits behind origin/develop with nothing surfacing the drift).
        # Each tick does a ``git fetch`` + ``rev-list`` (OFF the event loop
        # via ``_off_loop`` so a wedged fetch can never starve the bind) and,
        # when the checkout is behind origin/develop, FAILS LOUD: a warning
        # log + an anomaly on the ``scitex_cards.hooks`` bus. Sleeps before
        # the first tick. Honour SAC_DEPLOY_FRESHNESS_DISABLED=1 to skip.
        if os.environ.get(DEPLOY_FRESHNESS_ENV_DISABLED, "") != "1":
            df_task = asyncio.create_task(
                deploy_freshness_loop(
                    interval_s=_env_float(
                        DEPLOY_FRESHNESS_ENV_INTERVAL_S,
                        DEFAULT_DEPLOY_FRESHNESS_INTERVAL_S,
                    ),
                )
            )
            app.state.deploy_freshness_task = df_task
            tasks.append(df_task)

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

        # SIGTERM shutdown bridge (card sac-listen-sigterm-sse-shutdown-hang).
        # The SSE inbox-stream handlers park on ``queue.get()``; uvicorn's
        # graceful shutdown WAITS for those in-flight responses before it
        # even runs this lifespan's teardown, so a SIGTERM hangs the daemon
        # until ``sac listen restart --force`` SIGKILLs at 10 s. Uvicorn
        # sets ``server.should_exit`` SYNCHRONOUSLY in its signal handler,
        # well before that wait — so this task polls that flag and closes
        # the inbox broker the instant it flips. Closing the broker fires
        # the shutdown Event the SSE loops race ``queue.get()`` against, so
        # every in-flight stream returns at once, the connections drain,
        # and the daemon exits cleanly in well under the restart grace.
        #
        # The server handle is stashed on ``app.state.uvicorn_server`` by
        # the boot path (:func:`cli_pkg.listen_cmds._do_start_listen`). When
        # absent (in-process ASGI runs / tests with no uvicorn Server) the
        # bridge is skipped — the teardown ``finally`` below still closes
        # the broker so those runs free their SSE subscribers cleanly.
        _srv = getattr(app.state, "uvicorn_server", None)
        _broker = getattr(app.state, "inbox", None)
        if _srv is not None and _broker is not None:

            async def _shutdown_bridge(srv=_srv, broker=_broker):  # type: ignore[no-untyped-def]
                try:
                    while not getattr(srv, "should_exit", False):
                        await asyncio.sleep(0.1)
                finally:
                    # Close on the normal path AND on cancellation (the
                    # teardown cancels this task) so the broker is always
                    # signalled exactly once — ``close`` is idempotent.
                    broker.close()

            bridge_task = asyncio.create_task(_shutdown_bridge())
            app.state.shutdown_bridge_task = bridge_task
            tasks.append(bridge_task)

        try:
            yield
        finally:
            # Signal the SSE inbox-stream loops to stop FIRST (idempotent),
            # so an in-process lifespan shutdown with no uvicorn bridge
            # still frees any parked subscriber before we cancel the loops.
            _broker_td = getattr(app.state, "inbox", None)
            if _broker_td is not None:
                _broker_td.close()
            for _t in tasks:
                if _t is not None and not _t.done():
                    _t.cancel()
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):  # stx-allow: fallback (teardown best-effort; a loop's final exception must not block shutdown)
                        pass

    return _lifespan


__all__ = ["build_listen_lifespan"]
