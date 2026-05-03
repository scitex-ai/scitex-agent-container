"""Tests for ``NonceProbeAction``.

Exercises the four ``PaneAction`` methods in isolation plus the
end-to-end flow through :func:`run_action`, with all side-effecting
callables (``capture_fn`` / ``time_fn`` / ``sleep_fn`` / mux) faked.
"""

from __future__ import annotations

from typing import List

from scitex_agent_container._state.action_base import (
    ActionContext,
    ActionOutcome,
    run_action,
)
from scitex_agent_container.actions.nonce_probe import NonceProbeAction

# ── helpers (minimal; heavier machinery is in test_action_base) ─────────────


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
        self.keys_sent: list[tuple[str, tuple[str, ...]]] = []

    def send_keys(self, session, *keys, **_):
        self.keys_sent.append((session, tuple(keys)))

    def send_text_and_submit(self, session, text, **_):
        self.text_submits.append((session, text))


class _ScriptedPane:
    """Serve pre-scripted pane captures. When exhausted, repeat the last."""

    def __init__(self, seq: List[str]):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if not self.seq:
            return ""
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


def _ctx(*, pane_sequence, mux=None, agent="alpha", session="tmux-alpha"):
    return ActionContext(
        agent=agent,
        session=session,
        mux=mux or _FakeMux(),
        capture_fn=_ScriptedPane(pane_sequence),
    )


# ── unit tests: snapshot / precheck / is_complete ───────────────────────────


class TestSnapshot:
    def test_returns_pane_tail_last_4000_chars(self):
        big = "A" * 5000 + "TAIL"
        ctx = _ctx(pane_sequence=[big])
        snap = NonceProbeAction().snapshot(ctx)
        assert snap["pane_tail"].endswith("TAIL")
        assert len(snap["pane_tail"]) == 4000

    def test_empty_pane_yields_empty_tail(self):
        ctx = _ctx(pane_sequence=[""])
        snap = NonceProbeAction().snapshot(ctx)
        assert snap["pane_tail"] == ""


class TestPrecheck:
    def test_allows_quiet_pane(self):
        action = NonceProbeAction()
        assert action.precheck({"pane_tail": "> \nbypass permissions active\n"})

    def test_rejects_busy_pane(self):
        """'Working…' in the tail -> defer; don't interrupt an in-flight turn."""
        action = NonceProbeAction()
        assert not action.precheck({"pane_tail": "user msg\nWorking\u2026\n"})

    def test_rejects_ruminating_pane(self):
        action = NonceProbeAction()
        assert not action.precheck({"pane_tail": "Ruminating\u2026 about X\n"})

    def test_missing_pane_tail_is_treated_as_quiet(self):
        """Absent key means 'no evidence of busy' -> allow."""
        action = NonceProbeAction()
        assert action.precheck({})


class TestIsComplete:
    def test_two_or_more_nonce_occurrences_is_complete(self):
        action = NonceProbeAction(nonce="abc123")
        before = {"pane_tail": "nothing yet"}
        now = {"pane_tail": ("> Repeat abc123\nThe nonce is abc123 as you asked.\n")}
        assert action.is_complete(before, now)

    def test_prompt_only_is_not_complete(self):
        """One occurrence = just the prompt line. Not an echo."""
        action = NonceProbeAction(nonce="abc123")
        before = {"pane_tail": ""}
        now = {"pane_tail": "> Repeat abc123\n"}
        assert not action.is_complete(before, now)

    def test_no_nonce_yet_is_not_complete(self):
        """Before the action has minted a nonce (e.g. precheck-fail
        path where before_send never ran) is_complete must not
        claim success."""
        action = NonceProbeAction()  # nonce=None
        assert not action.is_complete({}, {"pane_tail": "anything"})


# ── before_send: nonce minting ───────────────────────────────────────────────


class TestBeforeSendNonce:
    def test_mints_nonce_if_not_set(self):
        action = NonceProbeAction()
        ctx = _ctx(pane_sequence=[""])
        assert action._nonce is None
        action.before_send(ctx)
        assert action._nonce is not None
        assert len(action._nonce) == 8  # default hex length
        assert ctx.extras["nonce"] == action._nonce

    def test_preserves_provided_nonce(self):
        """Operators pass a deterministic nonce for tests."""
        action = NonceProbeAction(nonce="CAFEBABE")
        ctx = _ctx(pane_sequence=[""])
        action.before_send(ctx)
        assert action._nonce == "CAFEBABE"
        assert ctx.extras["nonce"] == "CAFEBABE"


# ── end-to-end through run_action ───────────────────────────────────────────


class TestEndToEndViaRunAction:
    def test_happy_path_echoes_back_returns_success(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        # Captures over time:
        #   1. before-snapshot: quiet pane (precheck passes)
        #   2. after send: pane shows "Repeat DEAD1234" (one occurrence)
        #   3. next poll: pane shows echo (two occurrences)
        captures = [
            "> \nready\n",
            "> Repeat DEAD1234\n",
            "> Repeat DEAD1234\nDEAD1234\n",
        ]
        ctx = _ctx(pane_sequence=captures, mux=mux)
        attempt = run_action(
            NonceProbeAction(nonce="DEAD1234"),
            ctx,
            timeout_s=10,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.SUCCESS
        # Send issued exactly the prompt we expect.
        assert mux.text_submits == [("tmux-alpha", "Repeat DEAD1234")]
        # Nonce preserved in the attempt extras for forensic readers.
        assert attempt.extras.get("nonce") == "DEAD1234"

    def test_busy_pane_precondition_fail_no_send(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        ctx = _ctx(pane_sequence=["Working\u2026\n"], mux=mux)
        attempt = run_action(
            NonceProbeAction(nonce="FEEDFACE"),
            ctx,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.PRECONDITION_FAIL
        assert mux.text_submits == []
        # Nonce is NOT in extras because before_send never ran.
        assert "nonce" not in attempt.extras

    def test_no_echo_within_deadline_is_timeout(self, tmp_path):
        clk = _FakeClock()
        mux = _FakeMux()
        # Pane shows the prompt (1 occurrence) but never echoes.
        ctx = _ctx(
            pane_sequence=["> \nquiet\n", "> Repeat CAFED00D\n"],
            mux=mux,
        )
        attempt = run_action(
            NonceProbeAction(nonce="CAFED00D"),
            ctx,
            timeout_s=4,
            poll_interval_s=2,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert attempt.outcome is ActionOutcome.COMPLETION_TIMEOUT
        # Send did fire.
        assert mux.text_submits == [("tmux-alpha", "Repeat CAFED00D")]

    def test_fresh_nonce_per_run_when_not_provided(self, tmp_path):
        """Two independent instances mint different nonces."""
        clk = _FakeClock()
        a1 = NonceProbeAction()
        a2 = NonceProbeAction()
        run_action(
            a1,
            _ctx(pane_sequence=["quiet\n"]),
            timeout_s=0.1,
            poll_interval_s=0.1,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        run_action(
            a2,
            _ctx(pane_sequence=["quiet\n"]),
            timeout_s=0.1,
            poll_interval_s=0.1,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
            store_root=tmp_path,
        )
        assert a1._nonce is not None and a2._nonce is not None
        assert a1._nonce != a2._nonce
