"""Periodic-drive listen-loop integration.

Lead a2a ``7916f486929f44fb92d3aa1571cfa1d0`` / ``e2cd619055`` (2026-06-14):
PR #404 shipped the runtime-agnostic decision core (predicate +
content builder + sweep + envelope). This module is the GLUE:
the long-running asyncio task that the ``sac listen`` lifespan
launches at boot, ticks every ``tick_interval_s`` (default 60s),
and calls :func:`_periodic_drive.sweep` against the live agent
registry — emitting each due agent's drive envelope into the
listen-server's ``Broker`` (the SAME inbox a2a inbound traffic
flows through). Agents consume the envelope through their
existing a2a subscribe path; ``ClaudeSessionRuntime`` injects it
as the next SDK turn, ``TuiSessionRuntime`` lands it via
``send_turn`` → ``mux.send_text_and_submit``.

ONE mechanism for both runtimes. No TUI-only special case.

Cancellation: the task is parked in ``app.state.periodic_drive_task``;
the lifespan teardown cancels it cleanly so a ``sac listen`` SIGTERM
doesn't leak the loop.

Failure modes:
* Tick body raises → log + sleep + retry (the loop must not die).
* Per-agent emit failure → already handled inside ``sweep``; loop
  continues to the next agent.
* Registry unavailable → log + sleep + retry on the next tick.
* Cancellation → swallow ``asyncio.CancelledError`` cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

from ._periodic_drive import PeriodicDriveEnvelope, _AgentState, sweep

logger = logging.getLogger(__name__)

# How often the listen-side ticker wakes to invoke ``sweep``.
# ``sweep`` itself honours per-agent ``last_drive_at + interval_s``
# so this is JUST the polling-cadence floor — finer than the
# agent intervals so an agent's first-tick latency stays bounded.
DEFAULT_TICK_INTERVAL_S = 60.0


def _agents_from_registry(registry: Any) -> list[_AgentState]:
    """Resolve the live fleet's :class:`_AgentState` view from the
    in-process ``Registry``.

    Defensive against an evolving registry surface — we read only
    the public ``snapshot()`` + load each spec via ``load_config``.
    Per-agent failure (bad spec, missing worktree) is logged + the
    agent is skipped; one bad agent doesn't break the sweep.
    """
    from ..config import load_config
    from ..config._resolve import resolve_config

    out: list[_AgentState] = []
    try:
        snapshot = registry.snapshot() if registry is not None else []
    except Exception as exc:  # stx-allow: fallback (registry transient)
        logger.warning("periodic_drive_loop: registry snapshot failed: %s", exc)
        return out
    for row in snapshot or []:
        try:
            name = (
                row.get("name") if isinstance(row, dict) else getattr(row, "name", "")
            )
            if not name:
                continue
            status = (
                row.get("status")
                if isinstance(row, dict)
                else getattr(row, "status", "")
            ) or ""
            is_running = status.lower() == "running"
            spec_path = resolve_config(name)
            cfg = load_config(spec_path)
            drive_cfg = getattr(cfg, "periodic_drive", None)
            enabled = bool(getattr(drive_cfg, "enabled", True))
            interval_s = float(getattr(drive_cfg, "interval_s", 600.0))
            standing_rules = (
                getattr(cfg, "rules", "") or getattr(cfg, "standing_rules", "") or ""
            )
            mission = (
                getattr(cfg, "mission", "") or getattr(cfg, "description", "") or ""
            )
            workdir = getattr(cfg, "expanded_workdir", "") or getattr(
                cfg, "workdir", ""
            )
            out.append(
                _AgentState(
                    name=name,
                    is_running=is_running,
                    workdir=workdir,
                    branch=getattr(cfg, "branch", "") or "",
                    last_commit_subject="",
                    worktree_name=name,  # use the registry-name as proxy until git walk lands
                    standing_rules=standing_rules,
                    mission=mission,
                    last_drive_at=0.0,  # poller-local state in a real impl; tracked via state.db
                    interval_s=interval_s,
                    enabled=enabled,
                )
            )
        except Exception as exc:  # stx-allow: fallback (per-agent best-effort)
            logger.warning(
                "periodic_drive_loop: agent state resolve failed for %s: %s",
                row,
                exc,
            )
    return out


async def periodic_drive_loop(
    app_state: Any,
    *,
    tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    agents_source: "Iterable[_AgentState] | None" = None,
    now_fn=time.time,
) -> None:
    """Long-running task launched by the listen-server lifespan.

    Cadence: wakes every ``tick_interval_s`` seconds, calls
    :func:`_periodic_drive.sweep` with an emit hook bound to
    ``app_state.inbox.publish``. Each due agent receives a
    ``periodic_drive`` envelope on its a2a inbox; the agent's
    runtime delivers it as the next turn.

    ``agents_source`` is an injection seam — tests pass an iterable
    of :class:`_AgentState` directly; production callers leave it
    as ``None`` to pull from the in-process ``Registry``.

    Cancellation is honoured at the sleep boundary; the loop body
    itself never lingers, so SIGTERM propagation is bounded by
    ``tick_interval_s``.
    """
    logger.info(
        "periodic_drive_loop: starting (tick_interval_s=%.1f)",
        tick_interval_s,
    )
    last_emit_at: dict[str, float] = {}
    try:
        while True:
            try:
                if agents_source is not None:
                    agents = list(agents_source)
                else:
                    registry = getattr(app_state, "registry", None)
                    agents = _agents_from_registry(registry)

                # Fold the loop-local last_emit_at memory into each
                # state so the sweep's rate-limit reads the right
                # ``last_drive_at`` — the AgentState dataclass from
                # _periodic_drive carries it but the registry
                # resolver defaults it to 0.0; the loop owns
                # cross-tick rate-limit memory.
                for state in agents:
                    if state.name in last_emit_at:
                        state.last_drive_at = last_emit_at[state.name]

                inbox = getattr(app_state, "inbox", None)

                def _emit(envelope: PeriodicDriveEnvelope) -> None:
                    if inbox is None:
                        logger.warning(
                            "periodic_drive_loop: app.state.inbox is None; "
                            "envelope for %s dropped",
                            envelope.agent_name,
                        )
                        return
                    payload = {
                        "kind": envelope.kind,
                        "body": envelope.body,
                        "generated_at": envelope.generated_at,
                        "from_agent": "sac-periodic-drive",
                    }
                    # ``Broker.publish`` is async; schedule it via the
                    # running loop so emit stays a synchronous side-
                    # effect from sweep's POV.
                    asyncio.create_task(inbox.publish(envelope.agent_name, payload))
                    last_emit_at[envelope.agent_name] = envelope.generated_at

                emitted = sweep(agents, emit=_emit, now=now_fn())
                if emitted:
                    logger.info(
                        "periodic_drive_loop: emitted %d drive(s) this tick",
                        len(emitted),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must not die on a transient registry / spec error)
                logger.warning(
                    "periodic_drive_loop: tick failed (%s); sleeping + retry",
                    exc,
                )
            await asyncio.sleep(tick_interval_s)
    except asyncio.CancelledError:
        logger.info("periodic_drive_loop: cancelled cleanly")
        raise


__all__ = ["DEFAULT_TICK_INTERVAL_S", "periodic_drive_loop"]
