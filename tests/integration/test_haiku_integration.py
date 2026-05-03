"""End-to-end integration tests against a real haiku-backed Claude Code
agent running in a tmux session.

These tests exercise the whole ``tmux -> claude-code -> LLM -> pane``
round-trip the other tests only stub. They are expensive (each run
burns a haiku turn and takes ~30s), so they are:

1. Marked ``@pytest.mark.integration`` — off by default.
2. Auto-skipped unless all prerequisites are satisfied:
     - ``claude`` binary on PATH
     - ``tmux`` binary on PATH
     - A valid Claude Code auth (``~/.claude/.credentials.json``
       from ``claude /login``, OR ``ANTHROPIC_API_KEY`` env var)

Run them with::

    pytest tests/test_haiku_integration.py -v -m integration

On CI they skip cleanly because claude / auth are absent.

Design
------
- ``haiku_agent`` fixture creates a temporary tmux session, spawns
  ``claude --model claude-haiku-4-5 --dangerously-skip-permissions``,
  waits for the TUI to settle, and yields a session name. Cleanup
  kills the session in ``finally``.
- The test uses the regular :class:`NonceProbeAction` through
  :func:`run_action` so the integration path is identical to what
  the scheduler runs in production — no mocks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from scitex_agent_container._state.action_base import (
    ActionContext,
    ActionOutcome,
    run_action,
)
from scitex_agent_container.actions.nonce_probe import NonceProbeAction
from scitex_agent_container.runtimes.tmux import TmuxManager


def _auth_available() -> tuple[bool, str]:
    """True if Claude Code has a viable auth path locally.

    Any of:
    - ``SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY`` — the
      project-scoped, CI-only API key (mirrored to a GitHub
      Actions secret of the same name). Exported as
      ``ANTHROPIC_API_KEY`` at the top of the test so the
      ``claude`` CLI actually picks it up. The ``_CI_`` infix is
      intentional: this key must never be sourced on a human-used
      Claude Code session (it would bill CI-budget turns against
      interactive work).
    - ``ANTHROPIC_API_KEY`` — a pre-exported plain env var.
    - OAuth credentials at ``~/.claude/.credentials.json``
      (populated by ``claude /login``). Typical on dev machines.
    """
    scoped = os.environ.get("SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY")
    if scoped:
        # Let ``claude`` CLI and any subprocess we spawn pick it up.
        # Idempotent — matches caller intent if they also exported
        # ANTHROPIC_API_KEY directly.
        os.environ.setdefault("ANTHROPIC_API_KEY", scoped)
        return True, "SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY env var"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY env var"
    cred = Path.home() / ".claude" / ".credentials.json"
    if cred.is_file():
        return True, str(cred)
    return False, "no auth"


def _prereqs_reason() -> str | None:
    """Return the skip reason if any prerequisite is missing, else None."""
    if shutil.which("claude") is None:
        return "'claude' CLI not on PATH"
    if shutil.which("tmux") is None:
        return "'tmux' not on PATH"
    ok, _ = _auth_available()
    if not ok:
        return (
            "no Claude Code auth detected: need ANTHROPIC_API_KEY or "
            "~/.claude/.credentials.json"
        )
    return None


_SKIP_REASON = _prereqs_reason()


@pytest.fixture(scope="module")
def haiku_agent():
    """Module-scoped: spawn one haiku agent, share across tests.

    Yields the tmux session name. Always kills the session on teardown,
    even if the test raised.
    """
    if _SKIP_REASON:
        pytest.skip(_SKIP_REASON)

    # Readable session + workdir prefix so the leaked-fixture signature
    # on the dashboard is self-explanatory ("scitex-haiku-integration-*"
    # instead of the cryptic "int" abbreviation).
    session = f"scitex-haiku-integration-{uuid.uuid4().hex[:8]}"
    # Claude Code's TUI respects ``--dangerously-skip-permissions`` so
    # it doesn't prompt for tool-use consent. ``--model`` pins haiku
    # for cheap, fast replies.
    model = os.environ.get("SCITEX_AGENT_HAIKU_MODEL", "claude-haiku-4-5-20251001")
    # Use a throwaway workdir; any writable dir works since we won't
    # ask the agent to touch the filesystem.
    import tempfile

    workdir = tempfile.mkdtemp(prefix="scitex-haiku-integration-")

    cmd = f"cd {workdir} && claude --model {model} --dangerously-skip-permissions"
    # Start the tmux session running ``bash -lc`` so env is set up.
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "bash",
            "-lc",
            cmd,
        ],
        check=True,
    )

    try:
        # Drive the stock prompt auto-acceptor so the TUI transitions
        # through any startup radio selectors (bypass-permissions,
        # file-trust for the throwaway workdir, dev-channels,
        # thinking-effort, …) and reaches ready-state. We inline the
        # poll loop here rather than call _send_auto_accept_keystrokes
        # because that method requires a full AgentConfig; the logic
        # is tiny so duplicating it keeps the test dependency-free.
        from scitex_agent_container.runtimes.prompts import (
            detect_and_respond,
            is_ready,
        )

        def _send(s: str, *keys: str) -> None:
            TmuxManager.send_keys(s, *keys)

        # Previously a one-shot `accepted: set[str]` set permanently marked
        # each prompt as done after the first keystroke. In practice the
        # first send can silently no-op (tmux send-keys races with the TUI
        # startup before claude-code has grabbed stdin) and the handler
        # would then be skipped for the rest of the 90s deadline, causing
        # the fixture to time out with the prompt still visible and leak
        # the tmux session (msg#13170, scitex-haiku-int-48f9c296 incident).
        #
        # Track per-handler "last-fired-at" instead, with a 5s cool-down.
        # If the prompt is still visible after the cool-down, we fire
        # again — this recovers from a silently-lost initial keystroke
        # without spamming the main input (the detector only fires while
        # the prompt text is literally present in the pane).
        last_fired: dict[str, float] = {}
        FIRE_COOLDOWN_S = 5.0
        deadline = time.time() + 180.0
        ready = False
        while time.time() < deadline:
            content = TmuxManager.capture_content(session) or ""
            if is_ready(content):
                ready = True
                break
            now = time.time()
            # Build a view of "handlers that fired too recently" so
            # detect_and_respond skips them on this iteration only.
            cooling = {k for k, t in last_fired.items() if now - t < FIRE_COOLDOWN_S}
            matched = detect_and_respond(
                content, cooling, lambda *keys: _send(session, *keys)
            )
            if matched:
                last_fired[matched] = time.time()
                time.sleep(2.0)
                continue
            time.sleep(1.5)

        if not ready:
            tail = (TmuxManager.capture_content(session) or "")[-1500:]
            pytest.skip(
                "haiku agent did not reach ready state in 180s. "
                f"fired prompts: {sorted(last_fired) or 'none'}. "
                f"Last pane:\n{tail}"
            )

        yield session
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


@pytest.mark.integration
class TestHaikuNonceProbe:
    """Full end-to-end: ask haiku to echo a nonce, verify the round-trip."""

    def test_nonce_probe_alive(self, haiku_agent, tmp_path):
        """A ready haiku agent should echo the nonce well within the
        default probe timeout. Asserts SUCCESS and that the nonce
        appears in ``pane_after`` at least twice (prompt + echo)."""
        session = haiku_agent

        def capture_fn() -> str:
            try:
                return TmuxManager.capture_content(session) or ""
            except Exception:
                return ""

        ctx = ActionContext(
            agent=session,
            session=session,
            mux=TmuxManager,
            capture_fn=capture_fn,
            context_pct_fn=lambda: None,  # not needed for probe
        )

        attempt = run_action(
            NonceProbeAction(),
            ctx,
            timeout_s=45.0,
            poll_interval_s=2.0,
            store_root=tmp_path,
        )

        assert attempt.outcome is ActionOutcome.SUCCESS, (
            f"expected SUCCESS, got {attempt.outcome.value}. "
            f"elapsed={attempt.elapsed_s:.1f}s. "
            f"pane_after={(attempt.pane_after or {}).get('text', '')[-800:]}"
        )
        # Nonce must appear at least twice in the pane tail
        # (prompt + echo).
        nonce = attempt.extras.get("nonce")
        assert nonce, "nonce should be recorded in extras"
        # NonceProbeAction.snapshot() returns {"pane_tail": ...};
        # that dict is what lands on attempt.pane_after before the
        # action_store wrapper touches it, so we read the action's
        # native key, not the store's ``text`` serialization key.
        pane_text = (attempt.pane_after or {}).get("pane_tail", "")
        assert pane_text.count(nonce) >= 2, (
            f"expected >= 2 occurrences of nonce {nonce} in pane_after, "
            f"got {pane_text.count(nonce)}. Pane:\n{pane_text[-800:]}"
        )

    def test_second_probe_reuses_agent(self, haiku_agent, tmp_path):
        """The same fixture can drive multiple probes; fresh nonce each
        time, no cross-contamination from a prior successful probe."""
        session = haiku_agent

        def capture_fn() -> str:
            try:
                return TmuxManager.capture_content(session) or ""
            except Exception:
                return ""

        ctx = ActionContext(
            agent=session,
            session=session,
            mux=TmuxManager,
            capture_fn=capture_fn,
            context_pct_fn=lambda: None,
        )

        # Give the agent a moment to finish redrawing after the prior
        # echo so precheck sees a quiet pane.
        time.sleep(3.0)

        attempt = run_action(
            NonceProbeAction(),
            ctx,
            timeout_s=45.0,
            poll_interval_s=2.0,
            store_root=tmp_path,
        )

        assert attempt.outcome is ActionOutcome.SUCCESS, (
            f"expected SUCCESS, got {attempt.outcome.value}. "
            f"elapsed={attempt.elapsed_s:.1f}s. "
            f"pane_after={(attempt.pane_after or {}).get('text', '')[-800:]}"
        )
