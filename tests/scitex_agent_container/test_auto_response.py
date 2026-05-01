"""Tests for ``auto_response`` — scheduler / policy layer.

Policy decisions are pure functions, covered independently of the
scheduler loop. The scheduler itself is exercised one tick at a
time with a ``FakeClock`` + ``FakeCtxFactory`` so no real sleeping
/ subprocess work happens.
"""

from __future__ import annotations

from typing import List, Optional

from scitex_agent_container.action_base import (
    ActionContext,
)
from scitex_agent_container.auto_response import (
    AutoResponsePolicy,
    AutoResponseScheduler,
    SchedulerState,
    should_compact,
    should_probe_nonce,
)

# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeClock:
    """Deterministic clock: ``sleep(d)`` advances by ``d``."""

    def __init__(self, t0: float = 0.0):
        self.t = t0

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


class _FakeMux:
    def __init__(self):
        self.text_submits: list[tuple[str, str]] = []

    def send_keys(self, *a, **k):
        pass

    def send_text_and_submit(self, session, text, **_):
        self.text_submits.append((session, text))


class _StaticPaneSeq:
    """Pane capture sequence; sticks on last item."""

    def __init__(self, seq: List[str]):
        self.seq = list(seq)

    def __call__(self) -> str:
        if not self.seq:
            return ""
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


class _CtxFactory:
    """Create a fresh ActionContext for every tick.

    Ticks always get new ``pane_sequence`` and ``context_pct_sequence``
    iterators so "what the pane looked like right now" is independent
    of a prior tick's consumption."""

    def __init__(
        self,
        *,
        agent="alpha",
        session="tmux-alpha",
        mux: Optional[_FakeMux] = None,
        pane_seq: tuple[str, ...] = ("quiet\n",),
        context_pct: Optional[float] = None,
    ):
        self.agent = agent
        self.session = session
        self.mux = mux or _FakeMux()
        self.pane_seq = pane_seq
        self.context_pct = context_pct
        self.calls = 0

    def __call__(self) -> ActionContext:
        self.calls += 1
        cap = _StaticPaneSeq(list(self.pane_seq))
        ctx_pct = self.context_pct

        def _ctx_pct():
            return ctx_pct

        return ActionContext(
            agent=self.agent,
            session=self.session,
            mux=self.mux,
            capture_fn=cap,
            context_pct_fn=_ctx_pct,
        )


def _policy(**kw) -> AutoResponsePolicy:
    base = dict(
        probe_interval_s=100.0,
        probe_timeout_s=2.0,
        probe_poll_interval_s=0.5,
        compact_enabled=True,
        compact_threshold_pct=80.0,
        compact_min_interval_s=200.0,
        compact_timeout_s=4.0,
        compact_min_drop_pct=20.0,
        tick_interval_s=10.0,
    )
    base.update(kw)
    return AutoResponsePolicy(**base)


# ── pure policy functions ────────────────────────────────────────────────────


class TestShouldProbeNonce:
    def test_first_call_returns_true(self):
        """last_probe_at=0 plus any now>=interval -> probe."""
        assert should_probe_nonce(100.0, SchedulerState(), _policy())

    def test_too_soon_after_last_probe_false(self):
        s = SchedulerState(last_probe_at=50.0)
        assert not should_probe_nonce(60.0, s, _policy(probe_interval_s=100.0))

    def test_interval_elapsed_true(self):
        s = SchedulerState(last_probe_at=50.0)
        assert should_probe_nonce(151.0, s, _policy(probe_interval_s=100.0))


class TestShouldCompact:
    def test_disabled_policy_returns_false(self):
        assert not should_compact(
            100.0, SchedulerState(), _policy(compact_enabled=False), 95.0
        )

    def test_context_none_returns_false(self):
        """No signal -> cannot decide -> no action."""
        assert not should_compact(100.0, SchedulerState(), _policy(), context_pct=None)

    def test_context_below_threshold_false(self):
        assert not should_compact(
            100.0, SchedulerState(), _policy(compact_threshold_pct=80.0), 70.0
        )

    def test_at_threshold_true(self):
        """Boundary: >= threshold counts."""
        assert should_compact(
            1000.0, SchedulerState(), _policy(compact_threshold_pct=80.0), 80.0
        )

    def test_too_soon_after_last_compact_false(self):
        s = SchedulerState(last_compact_at=100.0)
        assert not should_compact(150.0, s, _policy(compact_min_interval_s=200.0), 95.0)

    def test_min_interval_elapsed_true(self):
        s = SchedulerState(last_compact_at=100.0)
        assert should_compact(500.0, s, _policy(compact_min_interval_s=200.0), 95.0)


# ── scheduler tick — what fires and when ────────────────────────────────────


class TestSchedulerTick:
    def test_first_tick_below_threshold_probes_only(self, tmp_path, monkeypatch):
        """Context below compact threshold -> only the probe fires."""
        from scitex_agent_container import action_store

        monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
        clk = _FakeClock(t0=1000.0)
        mux = _FakeMux()
        factory = _CtxFactory(
            mux=mux,
            pane_seq=("quiet\n",),  # triggers TIMEOUT (no echo),
            context_pct=50.0,  # below compact threshold
        )
        sched = AutoResponseScheduler(
            agent="alpha",
            ctx_factory=factory,
            policy=_policy(),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        attempts = sched.tick()
        assert len(attempts) == 1
        assert attempts[0].action == "nonce-probe"
        # One text_submit from the probe — "Repeat <nonce>".
        assert len(mux.text_submits) == 1
        assert mux.text_submits[0][1].startswith("Repeat ")

    def test_over_threshold_compacts_then_probes(self, tmp_path, monkeypatch):
        from scitex_agent_container import action_store

        monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
        clk = _FakeClock(t0=1000.0)
        mux = _FakeMux()
        factory = _CtxFactory(
            mux=mux,
            pane_seq=("quiet\n",),
            context_pct=85.0,  # over compact threshold
        )
        sched = AutoResponseScheduler(
            agent="alpha",
            ctx_factory=factory,
            policy=_policy(),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        attempts = sched.tick()
        # Compact first, then probe — both fired.
        assert [a.action for a in attempts] == ["compact", "nonce-probe"]
        # Two send_text_and_submit calls: "/compact" then "Repeat ...".
        assert mux.text_submits[0][1] == "/compact"
        assert mux.text_submits[1][1].startswith("Repeat ")

    def test_second_tick_within_intervals_fires_nothing(self, tmp_path, monkeypatch):
        """Back-to-back ticks inside both intervals must not re-fire."""
        from scitex_agent_container import action_store

        monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
        clk = _FakeClock(t0=1000.0)
        mux = _FakeMux()
        factory = _CtxFactory(mux=mux, pane_seq=("quiet\n",), context_pct=85.0)
        sched = AutoResponseScheduler(
            agent="alpha",
            ctx_factory=factory,
            policy=_policy(
                probe_interval_s=600.0,
                compact_min_interval_s=600.0,
                tick_interval_s=60.0,
            ),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        # Tick 1: both fire.
        sched.tick()
        # Advance a little (not enough to re-trigger intervals).
        clk.t += 60.0
        # Tick 2: neither should fire.
        attempts = sched.tick()
        assert attempts == []

    def test_state_records_timestamps_per_fired_action(self, tmp_path, monkeypatch):
        """After a successful tick, state reflects when each fired."""
        from scitex_agent_container import action_store

        monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
        clk = _FakeClock(t0=500.0)
        factory = _CtxFactory(pane_seq=("q\n",), context_pct=90.0)
        sched = AutoResponseScheduler(
            agent="alpha",
            ctx_factory=factory,
            policy=_policy(),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        sched.tick()
        assert sched.state.last_compact_at == 500.0
        assert sched.state.last_probe_at == 500.0


# ── run_forever — short-run termination works ──────────────────────────────


class TestRunForever:
    def test_stop_after_s_terminates_loop(self, tmp_path, monkeypatch):
        from scitex_agent_container import action_store

        monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
        clk = _FakeClock(t0=0.0)
        factory = _CtxFactory(pane_seq=("q\n",), context_pct=50.0)
        sched = AutoResponseScheduler(
            agent="alpha",
            ctx_factory=factory,
            policy=_policy(
                probe_interval_s=100.0,  # so each tick fires a probe
                tick_interval_s=10.0,
            ),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        # Stop after 25 fake-seconds: 3 ticks (at 0, 10, 20).
        sched.run_forever(stop_after_s=25.0)
        # First probe fires at tick 0 (probe_interval elapsed since
        # state.last_probe_at==0). Subsequent ticks within
        # probe_interval don't re-fire.
        assert sched.state.last_probe_at == 0.0
