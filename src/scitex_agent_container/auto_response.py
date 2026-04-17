"""Auto-response scheduler — policy layer over :mod:`action_base`.

Operators who want automated probing / compaction enable this
module. Those who do not never import it — the observer
(``liveness_probe``), the base class (``action_base``), and the
concrete actions all remain usable on their own.

Policy (all tunable)
--------------------
- Probe nonce liveness every ``probe_interval_s`` (default 600).
- Compact when ``context_pct >= compact_threshold_pct``
  (default 80) AND at least ``compact_min_interval_s`` have
  elapsed since the last compact attempt (default 900) to stop a
  flaky context reading from triggering repeat compacts.
- Never probe while the pane is busy: ``precheck`` already gates,
  so a busy agent returns ``PRECONDITION_FAIL`` (cheap; one
  attempt row logged).
- Never probe while the pane is mid-``Repeat <nonce>`` turn: the
  previous nonce is recorded in ``extras`` and a fresh probe that
  starts before the previous one completes is deliberately let
  through — the overlap is self-limiting because ``wait_for_nonce_
  echo`` just watches whichever nonce is currently in flight.

Design rules (reaffirmed)
-------------------------
- **No tight coupling to orochi.** The scheduler uses the local
  multiplexer + registry + action_store. It never talks to the hub.
  Consumers of the emitted attempts (dashboard, auditors) read
  orochi heartbeats independently.
- **No runtime post-hooks.** The scheduler writes one attempt per
  run and that is all. No callback fires to mutate defaults /
  raise timeouts — humans read the DB.
- **Injectable clocks.** ``time_fn``/``sleep_fn`` are constructor
  arguments so tests stay deterministic.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── Policy ──────────────────────────────────────────────────────────────────


@dataclass
class AutoResponsePolicy:
    """Tunables for the scheduler.

    Every value has a sensible default and an env-var override so
    operators can tune per-host without editing code.
    """

    probe_interval_s: float = float(
        os.environ.get("SCITEX_AGENT_PROBE_INTERVAL_S", "600")
    )
    probe_timeout_s: float = float(os.environ.get("SCITEX_AGENT_PROBE_TIMEOUT_S", "30"))
    probe_poll_interval_s: float = float(
        os.environ.get("SCITEX_AGENT_PROBE_POLL_INTERVAL_S", "2")
    )

    compact_enabled: bool = os.environ.get(
        "SCITEX_AGENT_COMPACT_ENABLED", "true"
    ).lower() in ("1", "true", "yes")
    compact_threshold_pct: float = float(
        os.environ.get("SCITEX_AGENT_COMPACT_THRESHOLD_PCT", "80")
    )
    compact_min_interval_s: float = float(
        os.environ.get("SCITEX_AGENT_COMPACT_MIN_INTERVAL_S", "900")
    )
    compact_timeout_s: float = float(
        os.environ.get("SCITEX_AGENT_COMPACT_TIMEOUT_S", "60")
    )
    compact_min_drop_pct: float = float(
        os.environ.get("SCITEX_AGENT_COMPACT_MIN_DROP_PCT", "20")
    )

    # Scheduler loop cadence — how often we evaluate whether to act.
    # Keep this >= 1s (log spam otherwise) and << the probe/compact
    # intervals so policy transitions are picked up promptly.
    tick_interval_s: float = float(
        os.environ.get("SCITEX_AGENT_AUTO_RESPONSE_TICK_S", "30")
    )


# ── Policy decisions (pure functions over state) ─────────────────────────────


@dataclass
class SchedulerState:
    """In-memory bookkeeping for the last action times.

    Intentionally not persisted to disk — a scheduler restart
    re-probes immediately (no harm) and defers compacts only until
    the threshold trips again (also no harm). Persistence would
    add complexity with no operational benefit.
    """

    last_probe_at: float = 0.0
    last_compact_at: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


def should_probe_nonce(
    now: float,
    state: SchedulerState,
    policy: AutoResponsePolicy,
) -> bool:
    """True iff enough time has elapsed since the last probe."""
    return (now - state.last_probe_at) >= policy.probe_interval_s


def should_compact(
    now: float,
    state: SchedulerState,
    policy: AutoResponsePolicy,
    context_pct: Optional[float],
) -> bool:
    """True iff compacting is enabled, context is over threshold,
    and the min-interval since last compact has elapsed."""
    if not policy.compact_enabled:
        return False
    if context_pct is None:
        return False  # cannot decide without signal
    if context_pct < policy.compact_threshold_pct:
        return False
    return (now - state.last_compact_at) >= policy.compact_min_interval_s


# ── Scheduler ───────────────────────────────────────────────────────────────


class AutoResponseScheduler:
    """Runs one tick per ``policy.tick_interval_s``.

    A tick evaluates ``should_probe_nonce`` and ``should_compact``;
    whichever is due fires a :class:`PaneAction` via
    :func:`run_action` and its attempt lands in ``action_store``.

    The scheduler is an object (not a free function) only so tests
    can exercise one tick at a time via :meth:`tick` without
    entering the forever-loop in :meth:`run_forever`.
    """

    def __init__(
        self,
        agent: str,
        ctx_factory: Callable[[], Any],
        policy: Optional[AutoResponsePolicy] = None,
        state: Optional[SchedulerState] = None,
        time_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        """
        Parameters
        ----------
        agent:
            Registry name of the agent to probe.
        ctx_factory:
            Zero-arg callable returning a fresh
            :class:`ActionContext`. Called once per tick so stale
            mux/session handles don't leak across ticks.
        policy / state:
            Override defaults for tests.
        time_fn / sleep_fn:
            Injectable clocks.
        """
        self.agent = agent
        self.ctx_factory = ctx_factory
        self.policy = policy or AutoResponsePolicy()
        self.state = state or SchedulerState()
        self._time = time_fn
        self._sleep = sleep_fn

    def tick(self) -> list[Any]:
        """Evaluate + fire any actions that are due this tick.

        Returns the list of :class:`ActionAttempt` records produced
        (possibly empty).
        """
        from .action_base import run_action
        from .actions.compact import CompactAction
        from .actions.nonce_probe import NonceProbeAction

        now = self._time()
        attempts: list[Any] = []
        ctx = self.ctx_factory()

        # Compact takes precedence over probe when both are due —
        # an over-threshold context usually coincides with a silent
        # agent, and compacting first keeps the subsequent probe
        # cheap.
        ctx_pct = None
        try:
            ctx_pct = ctx.context_pct_fn()
        except Exception:
            ctx_pct = None

        if should_compact(now, self.state, self.policy, ctx_pct):
            attempt = run_action(
                CompactAction(min_drop_pct=self.policy.compact_min_drop_pct),
                ctx,
                timeout_s=self.policy.compact_timeout_s,
                poll_interval_s=self.policy.probe_poll_interval_s,
                time_fn=self._time,
                sleep_fn=self._sleep,
            )
            attempts.append(attempt)
            self.state.last_compact_at = now

        if should_probe_nonce(now, self.state, self.policy):
            # Fresh ctx so capture_fn reads post-compact pane.
            probe_ctx = self.ctx_factory()
            attempt = run_action(
                NonceProbeAction(),
                probe_ctx,
                timeout_s=self.policy.probe_timeout_s,
                poll_interval_s=self.policy.probe_poll_interval_s,
                time_fn=self._time,
                sleep_fn=self._sleep,
            )
            attempts.append(attempt)
            self.state.last_probe_at = now

        return attempts

    def run_forever(
        self,
        stop_after_s: Optional[float] = None,
    ) -> None:
        """Run ticks until ``stop_after_s`` elapses (or forever).

        Tests pass a short ``stop_after_s`` so the loop terminates.
        Production runs call this with ``stop_after_s=None`` and
        rely on a signal / external supervisor to stop the process.
        """
        start = self._time()
        while True:
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "auto_response tick raised %s: %s", type(exc).__name__, exc
                )
            if stop_after_s is not None:
                if (self._time() - start) >= stop_after_s:
                    return
            self._sleep(self.policy.tick_interval_s)
