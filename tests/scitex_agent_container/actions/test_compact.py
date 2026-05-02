"""Tests for ``CompactAction``.

Validates the ``context_pct``-as-completion-signal path — this is
the shape other numeric-metric actions will follow, so the tests
pin the contract precisely.
"""

from __future__ import annotations

from typing import List, Optional

from scitex_agent_container.action_base import (
    ActionContext,
    ActionOutcome,
    run_action,
)
from scitex_agent_container.actions.compact import CompactAction

# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


class _FakeMux:
    def __init__(self):
        self.text_submits: list[tuple[str, str]] = []

    def send_keys(self, *a, **k):  # unused; present for interface parity
        pass

    def send_text_and_submit(self, session, text, **_):
        self.text_submits.append((session, text))


class _ScriptedPane:
    def __init__(self, seq: List[str]):
        self.seq = list(seq)

    def __call__(self) -> str:
        if not self.seq:
            return ""
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


class _ScriptedContextPct:
    """Return a pre-defined sequence of context_pct values."""

    def __init__(self, seq: List[Optional[float]]):
        self.seq = list(seq)

    def __call__(self) -> Optional[float]:
        if not self.seq:
            return None
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


def _ctx(
    *,
    pane_sequence=("quiet\n",),
    context_pct_sequence=(None,),
    mux=None,
    agent="alpha",
    session="tmux-alpha",
):
    return ActionContext(
        agent=agent,
        session=session,
        mux=mux or _FakeMux(),
        capture_fn=_ScriptedPane(list(pane_sequence)),
        context_pct_fn=_ScriptedContextPct(list(context_pct_sequence)),
    )


# ── snapshot ────────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_includes_pane_tail_and_context_pct(self):
        ctx = _ctx(pane_sequence=["pane text"], context_pct_sequence=[85.0])
        snap = CompactAction().snapshot(ctx)
        assert "pane text" in snap["pane_tail"]
        assert snap["context_pct"] == 85.0

    def test_none_context_pct_preserved(self):
        ctx = _ctx(pane_sequence=["x"], context_pct_sequence=[None])
        snap = CompactAction().snapshot(ctx)
        assert snap["context_pct"] is None

    def test_string_context_pct_coerced_to_float(self):
        """Statusline parsers sometimes emit strings; accept them."""
        ctx = _ctx(pane_sequence=["x"], context_pct_sequence=["82.3"])
        snap = CompactAction().snapshot(ctx)
        assert snap["context_pct"] == 82.3

    def test_pane_tail_truncated(self):
        big = "X" * 3000 + "TAIL"
        ctx = _ctx(pane_sequence=[big], context_pct_sequence=[50.0])
        snap = CompactAction().snapshot(ctx)
        assert len(snap["pane_tail"]) == 2000
        assert snap["pane_tail"].endswith("TAIL")


# ── precheck ────────────────────────────────────────────────────────────────


class TestPrecheck:
    def test_quiet_pane_allowed(self):
        assert CompactAction().precheck({"pane_tail": "> \nready\n"})

    def test_busy_pane_rejected(self):
        assert not CompactAction().precheck({"pane_tail": "Working\u2026\n"})


# ── is_complete ─────────────────────────────────────────────────────────────


class TestIsComplete:
    def test_drop_exceeds_threshold_true(self):
        a = CompactAction(min_drop_pct=20.0)
        assert a.is_complete({"context_pct": 85.0}, {"context_pct": 60.0})

    def test_drop_below_threshold_false(self):
        """85 -> 75 = 10pp drop, threshold 20pp -> not complete yet."""
        a = CompactAction(min_drop_pct=20.0)
        assert not a.is_complete({"context_pct": 85.0}, {"context_pct": 75.0})

    def test_drop_equal_to_threshold_true(self):
        """>= threshold counts as success (inclusive boundary)."""
        a = CompactAction(min_drop_pct=20.0)
        assert a.is_complete({"context_pct": 85.0}, {"context_pct": 65.0})

    def test_before_none_returns_false(self):
        """Can't confirm without a baseline."""
        a = CompactAction()
        assert not a.is_complete({"context_pct": None}, {"context_pct": 40.0})

    def test_after_none_returns_false(self):
        a = CompactAction()
        assert not a.is_complete({"context_pct": 80.0}, {"context_pct": None})

    def test_context_rising_is_not_complete(self):
        """Negative drop (pct increased) must not be treated as success."""
        a = CompactAction(min_drop_pct=20.0)
        assert not a.is_complete({"context_pct": 50.0}, {"context_pct": 70.0})

    def test_custom_min_drop_pct(self):
        a_strict = CompactAction(min_drop_pct=50.0)
        assert not a_strict.is_complete({"context_pct": 80.0}, {"context_pct": 50.0})
        assert a_strict.is_complete({"context_pct": 80.0}, {"context_pct": 25.0})


# ── send ────────────────────────────────────────────────────────────────────


class TestSend:
    def test_sends_compact_command(self):
        mux = _FakeMux()
        ctx = _ctx(pane_sequence=["x"], context_pct_sequence=[80.0], mux=mux)
        CompactAction().send(ctx)
        assert mux.text_submits == [("tmux-alpha", "/compact")]
        assert ctx.extras["command"] == "/compact"

    def test_custom_command_override(self):
        """A subclass or test may override the command string."""
        mux = _FakeMux()
        ctx = _ctx(pane_sequence=["x"], context_pct_sequence=[80.0], mux=mux)
        CompactAction(command="/compact --hard").send(ctx)
        assert mux.text_submits == [("tmux-alpha", "/compact --hard")]


# ── end-to-end through run_action ───────────────────────────────────────────


class TestEndToEndViaRunAction:
    def test_happy_path_context_drops_returns_success(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        # context_pct sequence:
        #   1. before-snapshot: 85
        #   2. poll 1 after send: still 80 (compact in progress)
        #   3. poll 2: 40 (compact finished -> SUCCESS)
        ctx = _ctx(
            pane_sequence=["quiet\n", "compacting\n", "done\n"],
            context_pct_sequence=[85.0, 80.0, 40.0],
            mux=mux,
        )
        attempt = run_action(
            CompactAction(min_drop_pct=20.0),
            ctx,
            timeout_s=30,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SUCCESS
        assert mux.text_submits == [("tmux-alpha", "/compact")]
        assert attempt.pane_before["context_pct"] == 85.0
        assert attempt.pane_after["context_pct"] == 40.0
        assert attempt.extras.get("command") == "/compact"

    def test_no_drop_within_deadline_is_timeout(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        # Context stays at 85 throughout -> timeout.
        ctx = _ctx(
            pane_sequence=["quiet\n"],
            context_pct_sequence=[85.0],  # stuck value
            mux=mux,
        )
        attempt = run_action(
            CompactAction(min_drop_pct=20.0),
            ctx,
            timeout_s=4,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.COMPLETION_TIMEOUT
        # Send did fire.
        assert mux.text_submits == [("tmux-alpha", "/compact")]

    def test_statusline_unavailable_cannot_confirm_times_out(self, tmp_path):
        """If context_pct is None throughout, the engine cannot
        confirm success and eventually times out. Attempt log still
        captures the attempt so operator sees the unavailable state."""
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(
            pane_sequence=["quiet\n"],
            context_pct_sequence=[None],
            mux=mux,
        )
        attempt = run_action(
            CompactAction(),
            ctx,
            timeout_s=2,
            poll_interval_s=1,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.COMPLETION_TIMEOUT
        assert attempt.pane_before["context_pct"] is None
        assert attempt.pane_after["context_pct"] is None

    def test_busy_pane_precondition_fail_no_send(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(
            pane_sequence=["Working\u2026 on prior turn\n"],
            context_pct_sequence=[85.0],
            mux=mux,
        )
        attempt = run_action(
            CompactAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.PRECONDITION_FAIL
        assert mux.text_submits == []
