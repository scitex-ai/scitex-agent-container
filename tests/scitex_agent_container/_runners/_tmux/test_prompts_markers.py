"""Day-1 salvage: re-audit the 10 (actually 12) auto-accept marker
detectors against the CURRENT ``claude`` TUI (v2.1.150) instead of
the May-2026 TUI they were originally written for.

The captured-pane fixtures embedded here come from a manual smoke
test run on 2026-06-12 with tmux 3.3a + claude 2.1.150 against a
fresh HOME. The aim is not full coverage — it's a regression net so
Day-2 work doesn't silently break a marker the salvage already
proved to match.

Markers we CAN exercise against captured screens:
- theme-selection (smoke-test screen 01 / 04 / 05 / 06 / 07)
- login-method (screen 02 / 03 / 05 / 07 / 08)

Markers we CANNOT exercise (post-auth gating, captured as DRIFT or
BLOCKED in day1-report.md):
- bypass-permissions, dev-channels, thinking-effort, mcp-json-edit,
  file-trust, file-trust-radio, external-imports, press-enter-continue,
  compose-pending-unsent, skip-permissions-yn
"""

from __future__ import annotations

import textwrap

import pytest

from scitex_agent_container._runners._tmux import prompts as P

# ---------------------------------------------------------------------------
# Captured screens (verbatim from /tmp/smoke-test-claude/screen-*.txt,
# claude v2.1.150 + tmux 3.3a, 2026-06-12).
# ---------------------------------------------------------------------------

THEME_SELECTION_SCREEN = textwrap.dedent(
    """\
    Welcome to Claude Code v2.1.150

     Let's get started.

     Choose the text style that looks best with your terminal
     To change this later, run /theme

       1. Auto (match terminal)
     ❯ 2. Dark mode ✔
       3. Light mode
       4. Dark mode (colorblind-friendly)
       5. Light mode (colorblind-friendly)
       6. Dark mode (ANSI colors only)
       7. Light mode (ANSI colors only)
    """
)

LOGIN_METHOD_SCREEN = textwrap.dedent(
    """\
    Welcome to Claude Code v2.1.150

     Claude Code can be used with your Claude subscription or billed based on API usage through your Console account.

     Select login method:

     ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
       2. Anthropic Console account · API usage billing
       3. 3rd-party platform · Amazon Bedrock, Microsoft Foundry, or Vertex AI
    """
)

OAUTH_PASTE_SCREEN = textwrap.dedent(
    """\
    Welcome to Claude Code v2.1.150

     Browser didn't open? Use the url below to sign in (c to copy)

    https://claude.com/cai/oauth/authorize?code=true&client_id=...

     Paste code here if prompted >
    """
)


# ---------------------------------------------------------------------------
# Positive: markers we observed in the live TUI must still detect.
# ---------------------------------------------------------------------------


def test_theme_selection_still_matches_v2_1_150():
    """theme-selection marker STILL-MATCHES on claude v2.1.150."""
    # Arrange
    content = THEME_SELECTION_SCREEN

    # Act
    matched = P._detect_theme_selection(content)

    # Assert
    assert matched is True


def test_login_method_still_matches_v2_1_150():
    """login-method marker STILL-MATCHES on claude v2.1.150.

    The 2026-06-12 TUI added option 3 ("3rd-party platform") but
    the detector matches on options 1 + 2, so it still fires.
    """
    # Arrange
    content = LOGIN_METHOD_SCREEN

    # Act
    matched = P._detect_login_method(content)

    # Assert
    assert matched is True


# ---------------------------------------------------------------------------
# Negative: markers must NOT fire on screens that aren't theirs.
# ---------------------------------------------------------------------------


def test_theme_selection_does_not_match_login_screen():
    # Arrange
    content = LOGIN_METHOD_SCREEN

    # Act
    matched = P._detect_theme_selection(content)

    # Assert
    assert matched is False


def test_login_method_does_not_match_theme_screen():
    # Arrange
    content = THEME_SELECTION_SCREEN

    # Act
    matched = P._detect_login_method(content)

    # Assert
    assert matched is False


def test_no_marker_fires_on_oauth_paste_screen():
    """OAuth paste-code prompt isn't in the handler list (DRIFT note in report).

    We assert here that none of the existing handlers accidentally
    fire on the OAuth screen, which would inject keystrokes into the
    paste-code field — a security-relevant regression.
    """
    # Arrange
    content = OAUTH_PASTE_SCREEN

    # Act
    fired = [h.name for h in P.PROMPT_HANDLERS if h.detect(content)]

    # Assert
    assert fired == [], f"Unexpected marker(s) fired on OAuth screen: {fired}"


# ---------------------------------------------------------------------------
# Registry shape: catch silent handler-list drift.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected_name",
    [
        "bypass-permissions",
        "dev-channels",
        "thinking-effort",
        "mcp-json-edit",
        "skip-permissions-yn",
        "press-enter-continue",
        "file-trust",
        "file-trust-radio",
        "theme-selection",
        "login-method",
        "compose-pending-unsent",
        "external-imports",
    ],
)
def test_handler_present_in_registry(expected_name: str):
    """All 12 salvaged handlers are present in PROMPT_HANDLERS.

    The original brief said 10; ba6755e^ actually carries 12 (the
    May-2026 source added compose-pending-unsent + external-imports
    + the radio variant of file-trust). Day-1 report records the
    discrepancy.
    """
    # Arrange
    handlers = P.PROMPT_HANDLERS
    # Act
    names = {h.name for h in handlers}
    # Assert
    assert expected_name in names


def test_compose_pending_detector_matches_nbsp_paste_gap():
    # Arrange — claude 2.1.150 renders the compose prompt gap as U+00A0 NBSP
    # (``❯\xa0[Pasted text …]``); the bare ``❯[ \t]+`` pattern missed it and a
    # pasted-but-unsent buffer went undetected (proj-scitex-dev 2026-06-23).
    pane = "❯\xa0[Pasted text #1 +26 lines]\n"
    # Act
    matched = P._detect_compose_pending_unsent(pane)
    # Assert
    assert matched is True
