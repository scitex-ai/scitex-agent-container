"""Zombie-agent reconciler: auto-clear stale ``running`` registrations.

Operator directive (2026-07-01): when the host SIF is rebuilt or the
``sac listen`` daemon restarts, an agent can become a ZOMBIE — the sac
registry still marks it ``running`` (an active ``instances`` lease row),
but there is NO live apptainer container behind it (its ``apptainer_pid``
file is gone or points at a reaped pid, and its a2a inbox is dead). A
plain ``sac agents restart`` then no-ops with "Agent X is already
running. No-op. Use --force" — so the dead agent stays dead until an
operator forces a fresh restart.

This module makes the fix DETERMINISTIC: a reconciler running inside the
listen daemon detects zombies (registry says running, no live container)
and CLEARS their stale registration/lease so the registry becomes
truthful again — a subsequent normal restart then relaunches cleanly.

SCOPE for this pass = **detect + clear** only. We do NOT auto-relaunch
and we do NOT emit bus events (both are deliberate follow-ons). Nothing
here deletes anything on disk beyond the stale ``instances`` lease row.

SEAMS / no-mocks (STX-NM002): every collaborator is injected as a plain
callable — ``is_container_alive`` (name -> bool oracle), ``running_oracle``
(-> list of names the registry marks running), and ``clear_lease``
(name -> clear its stale lease). Tests pass ordinary lambdas / dict-backed
closures, so the pure rule is trivially exercisable without any mock.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# --- env knobs --------------------------------------------------------------
# Gate: default ENABLED (operator directive — detection must be deterministic,
# not opt-in). Set SAC_ZOMBIE_RECONCILE_DISABLED=1 to skip launching the loop.
ENV_DISABLED = "SAC_ZOMBIE_RECONCILE_DISABLED"
ENV_INTERVAL_S = "SAC_ZOMBIE_RECONCILE_INTERVAL_S"

# Cadence: a zombie only appears on a rebuild/daemon-restart, so a modest
# interval reconciles it within a couple of minutes without hammering the
# registry. Reuses the liveness-tick cadence order of magnitude.
DEFAULT_INTERVAL_S = 120.0

__all__ = [
    "DEFAULT_INTERVAL_S",
    "ENV_DISABLED",
    "ENV_INTERVAL_S",
    "find_zombie_agents",
    "reconcile_zombies",
    "zombie_reconciler_loop",
    "production_is_container_alive",
    "production_running_oracle",
    "production_clear_lease",
]


def find_zombie_agents(
    running_agents: list[str],
    *,
    is_container_alive: Callable[[str], bool],
) -> list[str]:
    """Return the subset of ``running_agents`` with no live container.

    Pure function: an agent is a zombie iff the registry marks it running
    (it is in ``running_agents``) AND ``is_container_alive(name)`` is
    False. ``is_container_alive`` is the callable seam (name -> bool) —
    production wires it to the real apptainer-pid liveness check; tests
    pass a plain lambda / dict-lookup oracle.
    """
    return [name for name in running_agents if not is_container_alive(name)]


def reconcile_zombies(
    *,
    running_oracle: Callable[[], Iterable[str]],
    is_container_alive: Callable[[str], bool],
    clear_lease: Callable[[str], object],
    log: logging.Logger | None = None,
) -> list[str]:
    """Detect zombie agents and CLEAR each one's stale ``running`` lease.

    Resolves the currently-``running`` set via ``running_oracle``, runs
    the pure :func:`find_zombie_agents` rule against ``is_container_alive``,
    then for every zombie calls ``clear_lease(name)`` and logs a LOUD
    WARNING naming the agent (fail-loud: a zombie is a real anomaly the
    operator should see in the listen log). Returns the list of cleared
    names (empty when nothing was stale).

    All collaborators are injected callables (seams) so tests exercise the
    real branching with plain fakes — no mocks. A ``clear_lease`` that
    raises is logged and skipped: one un-clearable row must never abort
    the pass for the remaining zombies.
    """
    lg = log if log is not None else logger
    running = list(running_oracle())
    zombies = find_zombie_agents(running, is_container_alive=is_container_alive)
    cleared: list[str] = []
    for name in zombies:
        lg.warning(
            "zombie_reconciler: agent %r is marked RUNNING but has NO live "
            "apptainer container — clearing its stale registration so a "
            "normal restart can relaunch it cleanly",
            name,
        )
        try:
            clear_lease(name)
        except Exception as exc:  # stx-allow: fallback (one un-clearable lease must not abort the pass for the rest)
            lg.warning(
                "zombie_reconciler: failed to clear stale lease for %r "
                "(%s); leaving it for the next tick",
                name,
                exc,
            )
            continue
        cleared.append(name)
    return cleared


# ---------------------------------------------------------------------------
# Production seams (wired by the listen lifespan). Kept out of the pure
# functions above so tests never touch the real registry / FS.
# ---------------------------------------------------------------------------


def production_running_oracle() -> list[str]:
    """Names the registry currently marks ``running`` (active-lease rows).

    Reuses the existing ``instances`` reader (``ended_at IS NULL`` rows) —
    the same "one-at-a-time" lease the already-running check consults.
    Fail-soft: any registry hiccup ⇒ ``[]`` (nobody running this tick, so
    the reconciler clears nothing rather than acting on a bad read)."""
    try:
        from .._state.state_db import list_active_instances

        rows = list_active_instances(host=None)
    except Exception:  # stx-allow: fallback (registry transient → nothing reconciled this tick)
        return []
    names: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        try:
            name = str(row.get("name", "")).strip()
        except Exception:  # stx-allow: fallback (one bad row contributes nothing)
            continue
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def production_is_container_alive(name: str) -> bool:
    """True iff ``name`` has a live apptainer container on THIS host.

    A live container is proved by the runtime's ``apptainer_pid`` file:
    it must exist AND the pid it names must be alive (``os.kill(pid, 0)``).
    This mirrors the post-ack liveness probe's contract (a missing pid
    file ⇒ the runtime path was never taken; a reaped pid ⇒ the SIF came
    up and died) — exactly the two zombie shapes the operator described.

    Reuses ``state_dir_for`` (the per-agent runtime dir) and the shared
    ``_pid_alive`` helper. Fail-soft: any resolution error ⇒ False
    (treat as "no live container"), consistent with the stale-lease
    helper's "degrade to not-alive so a stuck lease does not pin forever".
    """
    from ._agent_exec_liveness import _pid_alive

    try:
        from .._runners.claude_session import state_dir_for
        from ..runtimes._apptainer_runtime import APPTAINER_PID_FILE

        pid_file: Path = state_dir_for(name) / APPTAINER_PID_FILE
        if not pid_file.is_file():
            return False
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    except Exception:  # stx-allow: fallback (unresolvable runtime dir ⇒ no live container)
        return False
    return pid > 0 and _pid_alive(pid)


def production_clear_lease(name: str) -> int:
    """Close ``name``'s stale active ``instances`` row(s) — the ``running``
    lease behind the false already-running signal.

    IMPORTANT (the reason we do NOT just call
    :func:`.._lifecycle._stale_lease.clear_stale_instance_lease`): that
    primitive is a pure "verify-pid + close" — it closes a row ONLY when
    the row's recorded ``pid`` is demonstrably dead, and it deliberately
    LEAVES NULL-pid rows alone (per-row proof of deadness is missing). But
    :func:`.._lifecycle._instances.record_local_instance` records the lease
    via ``record_instance_start`` WITHOUT a ``pid`` — so a zombie's active
    row has ``pid IS NULL`` and ``clear_stale_instance_lease`` would skip
    it, leaving the zombie pinned. The stale-lease docstring names exactly
    this: clearing a NULL-pid row is justified only by a
    "runtime-is-dead precondition" the CALLER must own.

    The reconciler IS that caller: it only invokes ``clear_lease`` AFTER
    ``production_is_container_alive`` proved (via the apptainer-pid file)
    that no live container exists. So here we close the active row(s)
    directly, reusing the SAME atomic primitives the stale-lease helper
    uses — ``list_active_instances`` (the read the already-running check
    and ``production_running_oracle`` both consult) + ``record_instance_stop``
    (which sets ``ended_at`` + writes a paired ``events`` row in one SQLite
    transaction). Once closed the agent reads NOT-running everywhere, so a
    normal restart relaunches it. Returns the number of rows cleared.

    Fail-soft: a missing/locked state.db degrades to 0 cleared (the next
    tick retries) rather than crashing the listen loop."""
    try:
        from .._state.state_db import (
            list_active_instances,
            record_instance_stop,
        )

        rows = list_active_instances(host=None)
    except Exception:  # stx-allow: fallback (missing/locked state.db → nothing cleared this tick; next tick retries)
        return 0
    cleared = 0
    for row in rows or []:
        if row.get("name") != name:
            continue
        row_id = row.get("id")
        if not row_id:
            continue
        try:
            if record_instance_stop(str(row_id), exit_reason="zombie-cleared"):
                cleared += 1
        except Exception:  # stx-allow: fallback (a failed row close must not abort the pass; next tick retries)
            continue
    return cleared


# ---------------------------------------------------------------------------
# Listen-daemon loop (launched by build_listen_lifespan). All three blocking
# collaborators — the registry read (running_oracle), the FS pid-file probe
# (is_container_alive), and the row-close (clear_lease) — run OFF the event
# loop via run_blocking_or so a slow/locked read can NEVER starve uvicorn's
# bind or the running server (same defense as the liveness-tick loop).
# ---------------------------------------------------------------------------


def _reconcile_once_blocking() -> list[str]:
    """One full detect+clear pass, using the production seams. BLOCKING —
    always dispatched off the loop by :func:`zombie_reconciler_loop`."""
    return reconcile_zombies(
        running_oracle=production_running_oracle,
        is_container_alive=production_is_container_alive,
        clear_lease=production_clear_lease,
    )


async def zombie_reconciler_loop(
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    reconcile_once: Callable[[], list[str]] | None = None,
) -> None:
    """Long-running task launched by the ``sac listen`` lifespan.

    Each tick runs one detect+clear pass OFF the event loop (bind-safe via
    :func:`.._lifecycle._off_loop.run_blocking_or`) and logs the cleared
    zombies. A tick that raises is logged and retried (the loop must not
    die); cancellation is honoured at the sleep boundary and re-raised
    cleanly — mirroring ``liveness_tick_reconciler_loop``'s teardown.

    ``reconcile_once`` is the test seam: pass a plain synchronous callable
    returning the cleared-names list to exercise the loop wiring without
    the registry/FS. Production leaves it ``None`` (uses the real pass).
    """
    fn = reconcile_once if reconcile_once is not None else _reconcile_once_blocking
    logger.info("zombie_reconciler: starting (interval_s=%.1f)", interval_s)
    try:
        while True:
            try:
                from .._lifecycle._off_loop import run_blocking_or

                cleared = await run_blocking_or(
                    fn,
                    default=[],
                    op="zombie_reconcile (registry read + apptainer-pid probe + lease close)",
                    timeout_s=max(interval_s, 15.0),
                )
                if cleared:
                    logger.warning(
                        "zombie_reconciler: cleared %d stale 'running' "
                        "registration(s) with no live container: %s",
                        len(cleared),
                        ", ".join(cleared),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must not die on a transient registry/FS error)
                logger.warning(
                    "zombie_reconciler: tick failed (%s); sleeping + retry", exc
                )
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("zombie_reconciler: cancelled cleanly")
        raise


def _env_interval_s() -> float:
    """Effective interval from the env (fail-soft to the default)."""
    try:
        return float(os.environ.get(ENV_INTERVAL_S, DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
