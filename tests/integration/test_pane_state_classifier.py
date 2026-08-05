"""Regression tests for ``_state.agent_meta._classify_pane_state``.

The classifier reads tmux pane tail and returns a ``(state, snippet)``
pair where ``state`` is a string label and ``snippet`` is the matched
context. We only assert on signals that come from **Claude Code itself** —
banners, error messages, prompt glyphs — not from optional statusline
tools like claude-hud (https://github.com/jarrodwatts/claude-hud) which
wouldn't be present in every install.

For state that needs claude-hud-style context-pct / tool-history info,
detection lives in the dashboard's ``_computeStateLocal`` (app.js) using
hub-side hook event fields (``last_tool_at``), not pane text. That is
the "use logic, not pattern matching" rule.

Behavioural surface pinned here:

* ``auth_error`` — every known wording variant from real Claude Code 2.x
  panes (current ``Please run /login``, legacy ``re-run /login``, HTTP
  401, JSON authentication_error payload, legacy ``Invalid API key``)
  plus a verbatim 2026-04-20 head-mba transcript whose snippet must
  carry an actionable signal for the operator.
* ``y_n_prompt`` — the four confirmation phrasings seen in the wild.
* ``compose_pending_unsent`` — fires for actual user text on a ``❯``
  prompt line but NOT for a bare ``❯`` followed by a decorative dashed
  separator (the prior ``❯\\s+\\S`` regex regression).
* ``limit_reached`` — quota-exhaustion banner.
* ``unknown`` / ``running`` — empty pane vs. bare prompt.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape invariants over a single arrange/act collapse into
``pytest.parametrize``.

No production changes. No mocks/monkeypatch — the classifier is a pure
function over pane text.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.agent_meta import _classify_pane_state

# ---------------------------------------------------------------------------
# auth_error: every known wording from real Claude Code 2.x panes
# ---------------------------------------------------------------------------


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
def test_auth_error_state_returned_for_known_marker_line(marker_line):
    """Each known auth-failure phrase, embedded in a minimal otherwise-
    legal pane, must trigger ``auth_error``. Pin every variant we see
    in the wild — when Claude Code rewords its error surface, add the
    new wording to this list, not by relaxing the matcher.
    """
    # Arrange
    pane = f"some preamble\n  ⎿  {marker_line}\n❯\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "auth_error", f"{marker_line!r} → {state!r}"


def test_auth_error_state_returned_for_real_head_mba_2026_04_20_capture():
    """Verbatim transcript line from head-mba pane on 2026-04-20 after
    the user DM'd 'hello' and Claude Code returned 401. Captured live;
    do not rewrite. If this test breaks, capture the new wording from
    a real failing agent and add it as a new fixture-line — do NOT
    weaken the classifier to make this pass.
    """
    # Arrange
    pane = (
        "  Listening for channel messages from: server:fleet-hub\n"
        "  ← fleet-hub · ywatanabe: hello\n"
        '  ⎿  Please run /login · API Error: 401 {"type":"error",'
        '"error":{"type":"authentication_error","message":'
        '"Invalid authentication credentials"},'
        '"request_id":"req_011CaE4U43Uo4X166mwtZtNf"}\n'
        "❯\n"
    )
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "auth_error", f"got {state!r}"


def test_auth_error_snippet_carries_actionable_signal_for_real_capture():
    """Companion to the state-label assertion: the snippet returned to
    the operator must contain at least one actionable token (the
    ``/login`` command, the HTTP status, the JSON error type, or the
    word ``credentials``) so the dashboard can route the operator
    straight to the fix.
    """
    # Arrange
    pane = (
        "  Listening for channel messages from: server:fleet-hub\n"
        "  ← fleet-hub · ywatanabe: hello\n"
        '  ⎿  Please run /login · API Error: 401 {"type":"error",'
        '"error":{"type":"authentication_error","message":'
        '"Invalid authentication credentials"},'
        '"request_id":"req_011CaE4U43Uo4X166mwtZtNf"}\n'
        "❯\n"
    )
    # Act
    _, snippet = _classify_pane_state(pane)
    # Assert
    assert any(
        m in snippet for m in ("/login", "401", "authentication_error", "credentials")
    ), f"unhelpful snippet: {snippet!r}"


# ---------------------------------------------------------------------------
# y/n prompt — defense-in-depth wording matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "yn_phrasing",
    [
        "Continue? (y/n)",
        "Proceed? [Y/n]",
        "Confirm? (yes/no)",
        "OK? [yes/no]",
    ],
)
def test_y_n_prompt_state_returned_for_each_confirmation_phrasing(yn_phrasing):
    """Defense-in-depth: each of the four confirmation phrasings the
    classifier supports must yield ``y_n_prompt`` — different prompt
    surfaces (Click, manual ``input``, third-party libraries) use
    different wordings and we want all four to halt the agent.
    """
    # Arrange
    pane = f"work output here\n{yn_phrasing}\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "y_n_prompt", f"{yn_phrasing!r} → {state!r}"


# ---------------------------------------------------------------------------
# compose_pending vs decorative separator
# ---------------------------------------------------------------------------


def test_compose_pending_not_returned_for_bare_prompt_with_separator_line():
    """Earlier ``❯\\s+\\S`` regex matched the decorative dashed
    separator below the empty prompt and lit ``compose_pending`` for
    every fresh agent. Tightened to require non-whitespace on the
    SAME line as ❯. This test pins the fix.
    """
    # Arrange
    pane = "──────────────\n❯ \n──────────────\n  some statusline content\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state != "compose_pending_unsent", f"got {state!r}"


def test_compose_pending_returned_for_user_text_on_prompt_line():
    """Defense-in-depth: when the user IS typing on the ``❯`` prompt
    line itself, classifier must still flag ``compose_pending_unsent``
    so the dashboard knows the agent is waiting on the operator to hit
    enter, not on Claude Code to think.
    """
    # Arrange
    pane = "❯ hello world\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "compose_pending_unsent", f"got {state!r}"


# ---------------------------------------------------------------------------
# limit_reached
# ---------------------------------------------------------------------------


def test_limit_reached_state_returned_for_quota_exhausted_banner():
    """The ``Limit reached, resets in ...`` banner that Claude Code
    prints when the account quota is exhausted must surface as
    ``limit_reached`` so the dashboard can present the reset window.
    """
    # Arrange
    pane = "  Limit reached, resets in 3h 47m\n❯\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "limit_reached"


# ---------------------------------------------------------------------------
# empty / unknown / bare-prompt fallbacks
# ---------------------------------------------------------------------------


def test_unknown_state_returned_for_empty_pane():
    """An empty pane carries no signal at all — the classifier must
    return ``unknown`` rather than guessing one of the live-agent
    states. The dashboard treats ``unknown`` as a soft signal and
    leaves the previous state visible.
    """
    # Arrange
    pane = ""
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "unknown"


def test_running_state_returned_for_bare_prompt_with_separator_only():
    """A bare ❯ prompt with nothing else legible classifies as
    ``running``. The dashboard upgrades / downgrades from there using
    hub-side hook event ages — pane classifier does not try to
    distinguish 'just booted' from 'idle after work' (that lives in
    ``app.js`` ``_computeStateLocal`` where ``last_tool_at`` is
    available).
    """
    # Arrange
    pane = "──────────────\n❯ \n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "running"
