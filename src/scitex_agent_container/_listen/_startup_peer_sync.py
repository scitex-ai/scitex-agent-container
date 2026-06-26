"""Best-effort ``comms_nodes`` peer-sync at ``sac listen`` startup —
launched AFTER uvicorn binds, off the event loop.

Why this module exists (INCIDENT 2026-06-26)
--------------------------------------------
``sac listen`` used to run a SYNCHRONOUS ``sac registry sync --all`` over
ssh to every static peer (``cli_pkg.listen_cmds._maybe_sync_on_start``)
*before* ``uvicorn.run`` was ever reached. That sync had no overall
timeout, so a single powered-off peer made the ssh call HANG, blocking
boot before the bind — port 7878 never bound, with NO error logged, and
the whole fleet lost agent-to-agent comms. PR #469's bind-watchdog could
not catch this: the watchdog lives inside ``create_app``, which runs
*after* the pre-bind sync.

The fix
-------
The bind must be impossible to block. uvicorn binds 7878 FIRST; the
peer-sync runs best-effort AFTER the server is serving, as a lifespan
task — exactly the pattern PR #469 used to background self-peer
persistence and the three startup loops.

Because :func:`cli_pkg._registry_sync.registry_sync_impl` is blocking ssh
(``subprocess.run``), not async, it is dispatched OFF the event loop via
``asyncio.to_thread`` and bounded by an overall ``wait_for`` — so even if
every per-peer ssh somehow wedged past its own ``ConnectTimeout`` /
``timeout=`` guard, it can never occupy the loop thread or block uvicorn's
bind.

Best-effort + fail-loud
-----------------------
Every failure mode (no config, opt-out, no static peers, per-peer ssh
failure, overall timeout) is logged and the listen keeps serving. A
timeout or peer failure emits a LOUD ``warning`` naming the cause — never
a silent hang, never a silent swallow.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Overall wall-clock ceiling for the whole post-bind peer sweep. The sweep
# is already bounded per-peer (``_registry_sync._PEER_SSH_TIMEOUT_S``) and
# given the same value as an internal budget; this outer ``wait_for`` is the
# last line of defence so the lifespan task itself can never hang the
# teardown. Generous: a healthy multi-peer sweep finishes in well under this.
DEFAULT_STARTUP_SYNC_TIMEOUT_S = 60.0


def _read_sync_on_start_flag(cfg) -> bool:
    """Return the ``comms_nodes.sync_on_start`` flag (default True).

    The flag is read by hand from the raw config dict because
    :class:`host_config.Config` only structures the blocks sac parses
    natively. Any read error degrades to True (the historical default) —
    a best-effort sync that runs is strictly safer than one silently
    skipped because the YAML had an unrelated quirk.
    """
    raw_path = getattr(cfg, "source_path", None)
    if raw_path is None or not raw_path.is_file():
        return True
    try:
        import yaml

        raw = yaml.safe_load(raw_path.read_text()) or {}
    except Exception:  # stx-allow: fallback (reason: a malformed/unreadable config must not crash listen startup; default to the historical sync-on-start=True.)
        return True
    comms_nodes_cfg = raw.get("comms_nodes")
    if isinstance(comms_nodes_cfg, dict):
        flag = comms_nodes_cfg.get("sync_on_start", True)
        if isinstance(flag, bool):
            return flag
    return True


def _run_registry_sync_all(*, budget_s: float) -> int:
    """Blocking helper: run the all-peers registry sync once.

    Imported lazily and called via ``asyncio.to_thread`` so its blocking
    ssh ``subprocess.run`` calls never touch the event-loop thread.
    ``budget_s`` is forwarded as the sweep's internal overall budget so an
    unreachable peer is skipped fast even inside the thread.
    """
    from ..cli_pkg._registry_sync import registry_sync_impl

    return registry_sync_impl(
        from_peer=None,
        to_peer=None,
        all_peers=True,
        dry_run=False,
        as_json=False,
        overall_budget_s=budget_s,
    )


async def sync_peers_on_listen_startup(
    *, timeout_s: float = DEFAULT_STARTUP_SYNC_TIMEOUT_S
) -> None:
    """Lifespan task: best-effort ``comms_nodes`` sync AFTER the bind.

    Mirrors the synchronous pre-bind logic that lived in
    ``cli_pkg.listen_cmds._maybe_sync_on_start`` — read the
    ``comms_nodes.sync_on_start`` opt-out, skip silently when there are no
    static peers (single-host installs) — but runs OFF the event loop and
    bounded so it can NEVER block uvicorn's bind.

    Never raises: a discovery/import/timeout failure logs at ``warning``
    and the listen keeps serving. PRESERVES INTENT — the peer-registry sync
    still happens (best-effort, after bind); it is not deleted.
    """
    try:
        from .._state.host_config import load

        cfg = load()
        if not _read_sync_on_start_flag(cfg):
            return
        # Only run when there is at least one static (non-glob) peer; skip
        # silently otherwise so single-host installs don't spam warnings.
        static_peers = [n for n in cfg.peers.keys() if not any(c in n for c in "*?[")]
        if not static_peers:
            return
    except Exception as exc:  # stx-allow: fallback (reason: a config-read crash must not block listen startup; degrade to no startup sync.)
        logger.warning(
            "startup peer-sync: config read failed (%r); skipping sync, "
            "listen continues serving",
            exc,
        )
        return

    # Reserve a slice of the overall timeout for the in-thread sweep so the
    # outer ``wait_for`` is the hard backstop, not the primary limiter.
    sweep_budget = max(1.0, timeout_s - 1.0)
    try:
        rc = await asyncio.wait_for(
            asyncio.to_thread(_run_registry_sync_all, budget_s=sweep_budget),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        # FAIL LOUD: the sweep exceeded its budget. The bind is unaffected
        # (this runs after it), but the operator must know the federated
        # graph may be stale — an unreachable peer is the usual cause.
        logger.warning(
            "startup peer-sync: all-peers comms_nodes sync exceeded %.0fs and "
            "was abandoned (an unreachable/stalled static peer is the usual "
            "cause). The listen IS serving; the federated peer view may be "
            "stale until the next sync. Check `comms_nodes.peers` reachability.",
            timeout_s,
        )
        return
    except Exception as exc:  # stx-allow: fallback (reason: never block/kill the listen on a startup-sync error; log loud and keep serving.)
        logger.warning(
            "startup peer-sync: all-peers comms_nodes sync failed (%r); listen "
            "continues serving with the pre-sync peer view",
            exc,
        )
        return

    if rc != 0:
        # Per-peer failures already logged their own loud [FAIL] lines via
        # the sync's text report; summarise here so the cause is visible in
        # the listen's own log stream too.
        logger.warning(
            "startup peer-sync: comms_nodes sync completed with peer "
            "failures (rc=%d) — see the per-peer [FAIL] lines above. The "
            "listen IS serving; unreachable peers were skipped, not retried.",
            rc,
        )


__all__ = [
    "DEFAULT_STARTUP_SYNC_TIMEOUT_S",
    "sync_peers_on_listen_startup",
]
