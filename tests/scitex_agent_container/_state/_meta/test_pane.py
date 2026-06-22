"""Tests for ``_state._meta.pane`` — tmux pane capture + classifier.

PS-202 src-tests mirror. Pure helpers use literal strings; shell-out
helpers (``_capture_pane``, ``_subagent_count_from_pane``,
``_pids_from_session`` — note: pids lives in resources.py) are covered
via the real ``subprocess_shim`` fake-binary fixture so no
``monkeypatch.setattr`` of ``subprocess.run`` is required.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state._meta.pane import (
    _SUBAGENT_MARKER_RE,
    _capture_pane,
    _classify_pane_state,
    _subagent_count_from_pane,
    parse_subagent_count_from_pane_text,
)

# --- parse_subagent_count_from_pane_text ---------------------------------


@pytest.mark.parametrize(
    "pane_text,expected",
    [
        ("3 local agents running", 3),
        ("1 local agent running", 1),
        ("5 local agents still running", 5),
        ("", 0),
        ("nothing relevant here", 0),
        ("local agent without trailer", 0),
    ],
)
def test_parse_subagent_count_returns_expected(pane_text, expected):
    # Arrange
    text = pane_text
    # Act
    actual = parse_subagent_count_from_pane_text(text)
    # Assert
    assert actual == expected


def test_subagent_marker_regex_is_case_insensitive():
    # Arrange
    pane = "7 LOCAL AGENTS RUNNING"
    # Act
    match = _SUBAGENT_MARKER_RE.search(pane)
    # Assert
    assert match is not None


# --- _classify_pane_state ------------------------------------------------


def test_classify_returns_unknown_for_empty_pane():
    # Arrange
    pane = ""
    # Act
    state, _snippet = _classify_pane_state(pane)
    # Assert
    assert state == "unknown"


def test_classify_detects_auth_error_marker():
    # Arrange
    pane = "some chatter\nPlease re-run /login to authenticate\n"
    # Act
    state, _snippet = _classify_pane_state(pane)
    # Assert
    assert state == "auth_error"


def test_classify_detects_limit_reached():
    # Arrange
    pane = "Anthropic rate limit reached for this account"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "limit_reached"


def test_classify_detects_y_n_prompt():
    # Arrange
    pane = "Proceed? (y/n)"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "y_n_prompt"


def test_classify_detects_compose_pending():
    # Arrange
    pane = "❯ hello there\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "compose_pending_unsent"


def test_classify_detects_running_for_bare_prompt():
    # Arrange
    pane = "history line\n❯\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "running"


def test_classify_detects_login_url_oauth_screen():
    # Arrange
    pane = (
        "Paste this URL to sign in:\n"
        "https://claude.ai/oauth/authorize?code=true\n"
        "Paste code here:"
    )
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state == "login_url"


def test_classify_login_url_snippet_carries_the_url():
    # Arrange
    pane = "sign in:\nhttps://claude.ai/oauth/authorize?code=true\npaste code"
    # Act
    _state, snippet = _classify_pane_state(pane)
    # Assert
    assert snippet == "https://claude.ai/oauth/authorize?code=true"


def test_classify_oauth_url_without_login_cue_is_not_login_url():
    # Arrange: an agent that merely prints an oauth link, with no login cue.
    pane = "history\nthe callback is https://x/oauth/cb for reference\ndone\n"
    # Act
    state, _ = _classify_pane_state(pane)
    # Assert
    assert state != "login_url"


# --- _subagent_count_from_pane + _capture_pane (real subprocess) ---------


def test_subagent_count_returns_zero_for_non_tmux_multiplexer():
    # Arrange
    multiplexer = "screen"
    # Act
    count = _subagent_count_from_pane("session", multiplexer=multiplexer)
    # Assert
    assert count == 0


def test_capture_pane_returns_empty_for_non_tmux_multiplexer():
    # Arrange
    multiplexer = "screen"
    # Act
    out = _capture_pane("session", multiplexer=multiplexer)
    # Assert
    assert out == ""


def test_capture_pane_returns_tmux_output_via_real_subprocess(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="pane content\n")
    # Act
    out = _capture_pane("mysession", multiplexer="tmux")
    # Assert
    assert out == "pane content\n"


def test_subagent_count_parses_tmux_capture_via_real_subprocess(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="2 local agents running\n")
    # Act
    count = _subagent_count_from_pane("mysession", multiplexer="tmux")
    # Assert
    assert count == 2


def test_capture_pane_truncates_output_to_max_chars(subprocess_shim):
    # Arrange
    payload = "x" * 200
    subprocess_shim.install("tmux", stdout=payload)
    # Act
    out = _capture_pane("s", multiplexer="tmux", max_chars=50)
    # Assert
    assert len(out) == 50
