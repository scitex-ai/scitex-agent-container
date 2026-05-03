"""Regression tests for ``_classify_pane_state``.

The classifier reads tmux pane tail and returns a string state label.
We only assert on signals that come from **Claude Code itself** —
banners, error messages, prompt glyphs — not from optional statusline
tools like claude-hud (https://github.com/jarrodwatts/claude-hud) which
wouldn't be present in every install.

For state that needs claude-hud-style context-pct / tool-history info,
detection lives in the dashboard's `_computeStateLocal` (app.js) using
hub-side hook event fields (`last_tool_at`), not pane text. That is
the "use logic, not pattern matching" rule.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.agent_meta import _classify_pane_state


# ----------------------------------------------------------------------
# auth_error: every known wording from real Claude Code 2.x panes
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "marker_line",
    [
        "Please run /login",  # Claude Code 2.1.x current wording
        "Please re-run /login",  # earlier Claude Code wording
        "API Error: 401 ... Invalid authentication credentials",
        '{"type":"authentication_error","message":"..."}',
        "Invalid API key · Please check",  # legacy
    ],
)
def test_auth_error_marker_variants(marker_line):
    """Each known auth-failure phrase, embedded in a minimal otherwise-
    legal pane, must trigger ``auth_error``. Pin every variant we see
    in the wild — when Claude Code rewords its error surface, add the
    new wording to this list, not by relaxing the matcher.
    """
    pane = f"some preamble\n  ⎿  {marker_line}\n❯\n"
    state, _ = _classify_pane_state(pane)
    assert state == "auth_error", f"{marker_line!r} → {state!r}"


def test_auth_error_real_capture_head_mba_2026_04_20():
    """Verbatim transcript line from head-mba pane on 2026-04-20 after
    the user DM'd 'hello' and Claude Code returned 401. Captured live;
    do not rewrite. If this test breaks, capture the new wording from
    a real failing agent and add it as a new fixture-line — do NOT
    weaken the classifier to make this pass.
    """
    pane = (
        "  Listening for channel messages from: server:scitex-orochi\n"
        "  ← scitex-orochi · ywatanabe: hello\n"
        '  ⎿  Please run /login · API Error: 401 {"type":"error",'
        '"error":{"type":"authentication_error","message":'
        '"Invalid authentication credentials"},'
        '"request_id":"req_011CaE4U43Uo4X166mwtZtNf"}\n'
        "❯\n"
    )
    state, snippet = _classify_pane_state(pane)
    assert state == "auth_error", f"got {state!r}"
    # Snippet should hand the operator the actionable signal.
    assert any(
        m in snippet for m in ("/login", "401", "authentication_error", "credentials")
    ), f"unhelpful snippet: {snippet!r}"


# ----------------------------------------------------------------------
# y/n prompt — defense-in-depth wording matrix
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "yn_phrasing",
    [
        "Continue? (y/n)",
        "Proceed? [Y/n]",
        "Confirm? (yes/no)",
        "OK? [yes/no]",
    ],
)
def test_y_n_prompt_variants(yn_phrasing):
    pane = f"work output here\n{yn_phrasing}\n"
    state, _ = _classify_pane_state(pane)
    assert state == "y_n_prompt", f"{yn_phrasing!r} → {state!r}"


# ----------------------------------------------------------------------
# compose_pending vs decorative separator
# ----------------------------------------------------------------------
def test_compose_pending_does_not_match_separator_line():
    """Earlier ``❯\\s+\\S`` regex matched the decorative dashed
    separator below the empty prompt and lit ``compose_pending`` for
    every fresh agent. Tightened to require non-whitespace on the
    SAME line as ❯. This test pins the fix.
    """
    pane = "──────────────\n❯ \n──────────────\n  some statusline content\n"
    state, _ = _classify_pane_state(pane)
    assert state != "compose_pending_unsent", f"got {state!r}"


def test_compose_pending_fires_for_actual_user_text():
    """Defense-in-depth: when the user IS typing, classifier must
    still flag compose_pending."""
    pane = "❯ hello world\n"
    state, _ = _classify_pane_state(pane)
    assert state == "compose_pending_unsent", f"got {state!r}"


# ----------------------------------------------------------------------
# limit_reached
# ----------------------------------------------------------------------
def test_limit_reached_marker():
    pane = "  Limit reached, resets in 3h 47m\n❯\n"
    state, _ = _classify_pane_state(pane)
    assert state == "limit_reached"


# ----------------------------------------------------------------------
# empty / unknown
# ----------------------------------------------------------------------
def test_empty_pane_unknown():
    state, _ = _classify_pane_state("")
    assert state == "unknown"


def test_running_when_only_prompt_visible():
    """A bare ❯ prompt with nothing else legible classifies as running.
    The dashboard upgrades / downgrades from there using hub-side
    hook event ages — pane classifier does not try to distinguish
    'just booted' from 'idle after work' (that lives in app.js
    _computeStateLocal where last_tool_at is available).
    """
    pane = "──────────────\n❯ \n"
    state, _ = _classify_pane_state(pane)
    assert state == "running"
