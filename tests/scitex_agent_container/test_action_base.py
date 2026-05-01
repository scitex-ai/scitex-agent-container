"""Tests for ``action_base`` — PaneAction ABC + run_action engine.

Covers all five terminal outcomes:

* ``SUCCESS``
* ``PRECONDITION_FAIL``
* ``SEND_ERROR``
* ``COMPLETION_TIMEOUT``
* ``SKIPPED_BY_POLICY``

plus attempt-record persistence to :mod:`action_store` and the
``before/now`` snapshot contract exposed to ``is_complete``.

No real sleeping or subprocess work — ``time_fn``/``sleep_fn``/
``capture_fn``/``context_pct_fn`` are all injected.
"""

from __future__ import annotations

from typing import Any

from scitex_agent_container.action_base import (
    ActionAttempt,
    ActionContext,
    ActionOutcome,
    PaneAction,
    run_action,
)
from scitex_agent_container.action_store import query

# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeClock:
    """Deterministic clock: ``sleep(d)`` advances time by ``d``."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


class _FakeMux:
    """Collects send-keys / send-text invocations for assertion."""

    def __init__(self):
        self.keys_sent: list[tuple[str, tuple[str, ...]]] = []
        self.text_submits: list[tuple[str, str]] = []

    def send_keys(self, session: str, *keys: str, **_) -> None:
        self.keys_sent.append((session, tuple(keys)))

    def send_text_and_submit(self, session: str, text: str, **_) -> None:
        self.text_submits.append((session, text))


class _CaptureSequence:
    """Return a list of pane captures in order (stick on last)."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if not self.seq:
            return ""
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


def _ctx(
    pane_sequence=("",),
    context_pct_sequence=(None,),
    mux=None,
    agent="alpha",
    session="tmux-alpha",
):
    cap = _CaptureSequence(pane_sequence)
    ctx_list = list(context_pct_sequence)

    def _ctx_pct():
        if not ctx_list:
            return None
        return ctx_list[0] if len(ctx_list) == 1 else ctx_list.pop(0)

    return ActionContext(
        agent=agent,
        session=session,
        mux=mux or _FakeMux(),
        capture_fn=cap,
        context_pct_fn=_ctx_pct,
    )


# ── sample PaneAction subclasses used only by these tests ──────────────────


class _AlwaysPassAction(PaneAction):
    """Precheck OK, send records the call, is_complete checks for a
    sentinel appearing in the pane tail."""

    name = "always-pass"

    def snapshot(self, ctx: ActionContext) -> dict[str, Any]:
        return {"tail": ctx.capture_fn()}

    def precheck(self, before: dict[str, Any]) -> bool:
        return True

    def send(self, ctx: ActionContext) -> None:
        ctx.mux.send_text_and_submit(ctx.session, "go")

    def is_complete(self, before, now) -> bool:
        return "DONE" in now.get("tail", "")


class _PrecheckFailAction(_AlwaysPassAction):
    name = "precheck-fail"

    def precheck(self, before):
        return False


class _SendRaisesAction(_AlwaysPassAction):
    name = "send-raises"

    def send(self, ctx):
        raise RuntimeError("tmux died")


class _NeverCompletesAction(_AlwaysPassAction):
    name = "never-completes"

    def is_complete(self, before, now):
        return False


class _ContextPctAction(PaneAction):
    """Models the CompactAction shape — completion = context dropped."""

    name = "context-drop"

    def snapshot(self, ctx):
        return {
            "tail": ctx.capture_fn()[-200:],
            "context_pct": ctx.context_pct_fn(),
        }

    def precheck(self, before):
        return True

    def send(self, ctx):
        ctx.mux.send_text_and_submit(ctx.session, "/compact")

    def is_complete(self, before, now):
        b = before.get("context_pct")
        n = now.get("context_pct")
        if b is None or n is None:
            return False
        return b - n >= 20


# ── outcome: SUCCESS ─────────────────────────────────────────────────────────


class TestSuccessOutcome:
    def test_completes_on_first_poll_returns_success(self, tmp_path):
        """Pane already shows DONE after send -> SUCCESS at t=0."""
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("DONE\n",))
        attempt = run_action(
            _AlwaysPassAction(),
            ctx,
            timeout_s=10,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SUCCESS
        assert attempt.elapsed_s == 0.0

    def test_completes_after_a_few_polls(self, tmp_path):
        """Pane changes over polls; completes on third capture."""
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("busy\n", "still\n", "DONE\n"))
        attempt = run_action(
            _AlwaysPassAction(),
            ctx,
            timeout_s=20,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SUCCESS
        # before-snapshot (1) + 3 polls -> 2 sleeps between polls, 2s each
        # The third poll triggers success.
        assert attempt.elapsed_s > 0

    def test_context_pct_action_success_on_drop(self, tmp_path):
        """CompactAction-shaped test: context goes 85 -> 60 -> SUCCESS."""
        clk = _FakeClock()
        ctx = _ctx(
            pane_sequence=("active\n",),
            context_pct_sequence=(85.0, 85.0, 60.0),  # before, during, after
        )
        attempt = run_action(
            _ContextPctAction(),
            ctx,
            timeout_s=30,
            poll_interval_s=1,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SUCCESS
        assert attempt.pane_before["context_pct"] == 85.0
        assert attempt.pane_after["context_pct"] == 60.0


# ── outcome: PRECONDITION_FAIL ──────────────────────────────────────────────


class TestPreconditionFail:
    def test_precheck_false_aborts_before_send(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(mux=mux)
        attempt = run_action(
            _PrecheckFailAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.PRECONDITION_FAIL
        assert mux.text_submits == []  # no send happened
        assert mux.keys_sent == []

    def test_precondition_fail_logs_before_snapshot(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("pre-state\n",))
        attempt = run_action(
            _PrecheckFailAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.pane_before is not None
        assert "pre-state" in attempt.pane_before["tail"]
        assert attempt.pane_after is None


# ── outcome: SEND_ERROR ─────────────────────────────────────────────────────


class TestSendError:
    def test_send_raises_produces_send_error(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx()
        attempt = run_action(
            _SendRaisesAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SEND_ERROR
        assert "tmux died" in attempt.extras.get("send_error", "")

    def test_send_error_does_not_poll(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("x\n",))
        attempt = run_action(
            _SendRaisesAction(),
            ctx,
            timeout_s=60,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # No time elapsed through sleep_fn.
        assert clk.t == 0.0
        assert attempt.outcome is ActionOutcome.SEND_ERROR


# ── outcome: COMPLETION_TIMEOUT ─────────────────────────────────────────────


class TestCompletionTimeout:
    def test_deadline_reached_without_complete(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("never-done\n",))
        attempt = run_action(
            _NeverCompletesAction(),
            ctx,
            timeout_s=4,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.COMPLETION_TIMEOUT
        assert attempt.elapsed_s >= 4.0
        # Both snapshots populated.
        assert attempt.pane_before is not None
        assert attempt.pane_after is not None


# ── outcome: SKIPPED_BY_POLICY ──────────────────────────────────────────────


class TestSkippedByPolicy:
    def test_skip_reason_short_circuits_before_precheck(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(mux=mux)
        # Use an action whose precheck would normally fail — the skip
        # must take precedence so we see SKIPPED not PRECONDITION_FAIL.
        attempt = run_action(
            _PrecheckFailAction(),
            ctx,
            skip_reason="recently-run",
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SKIPPED_BY_POLICY
        assert attempt.extras.get("skip_reason") == "recently-run"
        assert mux.text_submits == []


# ── store persistence ───────────────────────────────────────────────────────


class TestStorePersistence:
    def test_success_writes_one_row(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("DONE\n",))
        run_action(
            _AlwaysPassAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        rows = query(agent="alpha", root=tmp_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "always-pass"
        assert r["outcome"] == "success"
        assert r["pane_before"] == {"format": "json-dump", "text": '{"tail": ""}'} or r[
            "pane_before"
        ]["format"] in ("json-dump", "full")
        assert r["pane_after"] is not None

    def test_write_to_store_false_skips_row(self, tmp_path):
        clk = _FakeClock()
        ctx = _ctx(pane_sequence=("DONE\n",))
        run_action(
            _AlwaysPassAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
            write_to_store=False,
        )
        rows = query(root=tmp_path)
        assert rows == []

    def test_every_outcome_writes_a_row(self, tmp_path):
        """Five outcomes in a row -> five DB rows, each with the
        correct outcome string."""
        clk = _FakeClock()

        # SUCCESS
        run_action(
            _AlwaysPassAction(),
            _ctx(pane_sequence=("DONE\n",)),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # PRECONDITION_FAIL
        run_action(
            _PrecheckFailAction(),
            _ctx(),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # SEND_ERROR
        run_action(
            _SendRaisesAction(),
            _ctx(),
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # COMPLETION_TIMEOUT
        run_action(
            _NeverCompletesAction(),
            _ctx(pane_sequence=("x\n",)),
            timeout_s=1,
            poll_interval_s=1,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # SKIPPED_BY_POLICY
        run_action(
            _AlwaysPassAction(),
            _ctx(),
            skip_reason="testing",
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        rows = query(root=tmp_path, limit=100)
        outcomes = {r["outcome"] for r in rows}
        assert outcomes == {
            "success",
            "precondition_fail",
            "send_error",
            "completion_timeout",
            "skipped_by_policy",
        }


# ── send sequencing ─────────────────────────────────────────────────────────


class TestSendSequencing:
    def test_send_called_exactly_once_on_success(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(mux=mux, pane_sequence=("DONE\n",))
        run_action(
            _AlwaysPassAction(),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert mux.text_submits == [("tmux-alpha", "go")]

    def test_send_called_exactly_once_on_timeout(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(mux=mux, pane_sequence=("busy\n",))
        run_action(
            _NeverCompletesAction(),
            ctx,
            timeout_s=4,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        # Send fired once; polling then timed out.
        assert len(mux.text_submits) == 1


# ── ActionAttempt ────────────────────────────────────────────────────────────


class TestActionAttempt:
    def test_as_store_record_has_required_keys(self):
        a = ActionAttempt(
            agent="a",
            action="x",
            outcome=ActionOutcome.SUCCESS,
            elapsed_s=1.2,
            started_at="2026-04-17T00:00:00+00:00",
            pane_before={"format": "full", "text": "b"},
            pane_after={"format": "full", "text": "n"},
            extras={"nonce": "abc"},
        )
        rec = a.as_store_record()
        for k in ("ts", "agent", "action", "outcome", "elapsed_s"):
            assert k in rec
        assert rec["outcome"] == "success"
        assert rec["extras"] == {"nonce": "abc"}
