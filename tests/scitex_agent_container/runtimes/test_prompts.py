"""Tests for prompts.py — TUI prompt detection handlers."""

import pytest

from scitex_agent_container.runtimes.prompts import (
    PROMPT_HANDLERS,
    PromptHandler,
    _detect_bypass_permissions,
    _detect_dev_channels,
    _detect_file_trust,
    _detect_file_trust_radio,
    _detect_login_method,
    _detect_mcp_json_edit,
    _detect_press_enter_continue,
    _detect_skip_permissions_yn,
    _detect_theme_selection,
    _detect_thinking_effort,
    detect,
    detect_and_respond,
    is_ready,
    register_prompt,
    respond_modal,
)

# ── Startup handlers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "detector, content",
    [
        (
            _detect_bypass_permissions,
            "Bypass Permissions\n2. Yes, I accept\nEnter to confirm",
        ),
        (
            _detect_dev_channels,
            "1. I am using this for local development\n2. Exit\nEnter to confirm",
        ),
        (
            _detect_thinking_effort,
            "1. * Medium (recommended)\nthinking effort\nEnter to confirm",
        ),
        (_detect_skip_permissions_yn, "skip-permissions y/n"),
    ],
)
def test_startup_handler_matches_expected_prompt(detector, content):
    # Arrange
    payload = content
    # Act
    result = detector(payload)
    # Assert
    assert result is True


def test_bypass_permissions_no_match():
    # Arrange
    content = "Normal chat text"
    # Act
    result = _detect_bypass_permissions(content)
    # Assert
    assert result is False


# ── Runtime handlers ──────────────────────────────────────────────────────────


def test_mcp_json_edit_match():
    # Arrange
    content = ".mcp.json\n1. Yes, proceed\nEnter to confirm"
    # Act
    result = _detect_mcp_json_edit(content)
    # Assert
    assert result is True


def test_mcp_json_edit_no_match_without_confirm():
    # Arrange
    content = ".mcp.json updated successfully"
    # Act
    result = _detect_mcp_json_edit(content)
    # Assert
    assert result is False


def test_press_enter_continue_match():
    # Arrange
    content = "Context window approaching limit.\nPress Enter to continue"
    # Act
    result = _detect_press_enter_continue(content)
    # Assert
    assert result is True


def test_press_enter_continue_no_match_active():
    # Arrange
    content = "Working\u2026 Press Enter to continue"
    # Act
    result = _detect_press_enter_continue(content)
    # Assert
    assert result is False


def test_press_enter_continue_no_match_radio():
    # Arrange
    content = "1. Option A\n2. Option B\nEnter to confirm\nPress Enter to continue"
    # Act
    result = _detect_press_enter_continue(content)
    # Assert
    assert result is False


def test_press_enter_continue_only_checks_tail():
    # Arrange
    # Old scrollback + current idle prompt should NOT trigger
    # (strict last-5-lines window: the last visible line is the shell prompt)
    old = "Press Enter to continue\n" * 50
    current = old + "Some output\nMore output\nAnd more\nAnd even more\n\u276f "
    # Act
    result = _detect_press_enter_continue(current)
    # Assert
    assert result is False


def test_file_trust_radio_match():
    """New-style radio variant of the trust prompt (Claude Code >= 2.1.x)."""
    # Arrange
    content = (
        "Is this a project you created or one you trust?\n"
        "1. Yes, I trust this folder\n"
        "2. No, exit\n"
        "Enter to confirm - Esc to cancel"
    )
    # Act
    result = _detect_file_trust_radio(content)
    # Assert
    assert result is True


def test_file_trust_radio_no_match_without_both_options():
    """Must not fire on the bypass-permissions dialog (which also says
    'Enter to confirm' and has '2. Yes, I accept' but not the trust
    option strings)."""
    # Arrange
    content = "Bypass Permissions\n1. No, exit\n2. Yes, I accept\nEnter to confirm"
    # Act
    result = _detect_file_trust_radio(content)
    # Assert
    assert result is False


def test_theme_selection_match():
    """First-run theme prompt on a fresh HOME (e.g. CI). Must match the
    exact strings Claude Code emits so we don't fire on unrelated text."""
    # Arrange
    content = (
        "Let's get started.\n"
        "Choose the text style that looks best with your terminal\n"
        "1. Auto (match terminal)\n"
        "2. Dark mode\n"
        "3. Light mode\n"
    )
    # Act
    result = _detect_theme_selection(content)
    # Assert
    assert result is True


def test_login_method_match():
    """Fresh-HOME login picker: present before ANTHROPIC_API_KEY is
    honored even when set in env. Option 2 (Anthropic Console) is the
    right choice for the API-key auth path."""
    # Arrange
    content = (
        "Claude Code can be used with your Claude subscription or billed\n"
        "Select login method:\n"
        "1. Claude account with subscription\n"
        "2. Anthropic Console account · API usage billing\n"
        "3. 3rd-party platform\n"
    )
    # Act
    result = _detect_login_method(content)
    # Assert
    assert result is True


def test_login_method_no_match_on_plain_text():
    """Don't fire on a user message that just mentions 'login method'."""
    # Arrange
    content = "How do I change my login method?"
    # Act
    result = _detect_login_method(content)
    # Assert
    assert result is False


def test_theme_selection_no_match_on_theme_command_output():
    """Firing on `/theme` command output would re-select the theme
    mid-session. Require the "Let's get started"-adjacent wording."""
    # Arrange
    content = "1. Auto (match terminal)\n"
    # Act
    result = _detect_theme_selection(content)
    # Assert
    assert result is False


def test_file_trust_match():
    # Arrange
    content = "Do you trust the files in this folder? yes/no"
    # Act
    result = _detect_file_trust(content)
    # Assert
    assert result is True


def test_file_trust_no_match_no_folder():
    # Arrange
    content = "Do you trust this?"
    # Act
    result = _detect_file_trust(content)
    # Assert
    assert result is False


# ── Registry ─────────────────────────────────────────────────────────────────


def test_all_handlers_present():
    # Arrange
    expected = {
        "bypass-permissions",
        "dev-channels",
        "thinking-effort",
        "mcp-json-edit",
        "skip-permissions-yn",
        "press-enter-continue",
        "file-trust",
    }
    # Act
    names = {h.name for h in PROMPT_HANDLERS}
    # Assert
    assert expected <= names


def test_handlers_sorted_by_priority():
    # Arrange
    handlers = PROMPT_HANDLERS
    # Act
    priorities = [h.priority for h in handlers]
    # Assert
    assert priorities == sorted(priorities)


# ── detect_and_respond ───────────────────────────────────────────────────────


def test_detect_and_respond_returns_matched_handler_name():
    # Arrange
    sent: list[str] = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    # Act
    result = detect_and_respond(content, set(), lambda k: sent.append(k))
    # Assert
    assert result == "bypass-permissions"


def test_detect_and_respond_sends_option_key():
    # Arrange
    sent: list[str] = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    # Act
    detect_and_respond(content, set(), lambda k: sent.append(k))
    # Assert
    assert "2" in sent


def test_detect_and_respond_sends_enter_key():
    # Arrange
    sent: list[str] = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    # Act
    detect_and_respond(content, set(), lambda k: sent.append(k))
    # Assert
    assert "Enter" in sent


def test_detect_and_respond_skips_accepted_returns_none():
    # Arrange
    sent: list[str] = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    # Act
    result = detect_and_respond(
        content, {"bypass-permissions"}, lambda k: sent.append(k)
    )
    # Assert
    assert result is None


# Verbatim pane captured from a live in-apptainer TUI agent (2026-06-15)
# sitting on the bypass-permissions picker — guards the real screen
# format, not just the minimal synthetic anchors above.
_LIVE_BYPASS_PANE = """\
  WARNING: Claude Code running in Bypass Permissions mode

  In Bypass Permissions mode, Claude Code will not ask for your approval before running
   potentially dangerous commands.

  By proceeding, you accept all responsibility for actions taken while running in
  Bypass Permissions mode.

  https://code.claude.com/docs/en/security

  ❯ 1. No, exit
    2. Yes, I accept

  Enter to confirm · Esc to cancel
"""


def test_detect_and_respond_handles_live_bypass_pane():
    # Arrange
    sent: list[str] = []
    # Act
    result = detect_and_respond(_LIVE_BYPASS_PANE, set(), lambda k: sent.append(k))
    # Assert
    assert result == "bypass-permissions"


def test_live_bypass_pane_sends_two_then_enter():
    # Arrange
    sent: list[str] = []
    # Act
    detect_and_respond(_LIVE_BYPASS_PANE, set(), lambda k: sent.append(k))
    # Assert
    assert sent == ["2", "Enter"]


def test_live_bypass_pane_is_not_ready_while_modal_up():
    # Arrange — the bypass modal carries "Enter to confirm".
    pane = _LIVE_BYPASS_PANE
    # Act
    ready = is_ready(pane)
    # Assert — must NOT report ready while a blocking modal is showing.
    assert ready is False


def test_detect_and_respond_skips_accepted_sends_nothing():
    # Arrange
    sent: list[str] = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    # Act
    detect_and_respond(content, {"bypass-permissions"}, lambda k: sent.append(k))
    # Assert
    assert sent == []


# ── Timing / sequencing scenarios ─────────────────────────────────────────────
# The user's real-world pain point is auto-accept through a multi-prompt
# TUI boot sequence where prompts arrive one after another on a noisy pane
# that the auto-accept loop is polling. These tests simulate the full flow
# without needing a real tmux pane.


def _simulate_sequence(pane_sequence):
    """Feed a list of pane captures through detect_and_respond and
    accumulate the per-step outcome + aggregated accepted set + key log.

    This is the deterministic analog of the pane-polling loop the
    auto-accept runtime actually performs: each pane capture is
    one iteration.
    """
    accepted: set[str] = set()
    sent: list[str] = []
    matches: list[str | None] = []
    for content in pane_sequence:
        name = detect_and_respond(content, accepted, lambda k: sent.append(k))
        if name:
            accepted.add(name)
        matches.append(name)
    return matches, accepted, sent


# Realistic boot sequence shared inputs.
_BYPASS = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
_DEV = "1. I am using this for local development\n2. Exit\nEnter to confirm"
_THINKING = "1. Medium (recommended)\nthinking effort\nEnter to confirm"
_READY = "> \nbypass permissions active\n"


def test_realistic_boot_sequence_matches_in_priority_order():
    """Simulates a Claude Code boot that walks through three prompts
    (bypass-permissions -> dev-channels -> thinking-effort), then
    settles at the main prompt. Each handler fires exactly once, in
    priority order, and the final ready pane triggers nothing."""
    # Arrange
    sequence = [_BYPASS, _DEV, _THINKING, _READY]
    # Act
    matches, _, _ = _simulate_sequence(sequence)
    # Assert
    assert matches == [
        "bypass-permissions",
        "dev-channels",
        "thinking-effort",
        None,
    ]


def test_realistic_boot_sequence_accumulates_accepted_set():
    # Arrange
    sequence = [_BYPASS, _DEV, _THINKING, _READY]
    # Act
    _, accepted, _ = _simulate_sequence(sequence)
    # Assert
    assert accepted == {"bypass-permissions", "dev-channels", "thinking-effort"}


def test_realistic_boot_sequence_sends_each_handlers_keys_in_order():
    # Arrange
    sequence = [_BYPASS, _DEV, _THINKING, _READY]
    # Act
    _, _, sent = _simulate_sequence(sequence)
    # Assert
    assert sent == ["2", "Enter", "1", "Enter", "1", "Enter"]


def test_redraw_of_same_prompt_matches_only_first_time():
    """A slow pane may redraw the same prompt across several polls
    before the keystrokes are processed. Once we've accepted a
    prompt, re-seeing it must be a no-op — double-accept would
    press keys into the ready prompt and corrupt the next message."""
    # Arrange
    sequence = [_BYPASS, _BYPASS, _BYPASS]
    # Act
    matches, _, _ = _simulate_sequence(sequence)
    # Assert
    assert matches == ["bypass-permissions", None, None]


def test_redraw_of_same_prompt_sends_keys_only_once():
    # Arrange
    sequence = [_BYPASS, _BYPASS, _BYPASS]
    # Act
    _, _, sent = _simulate_sequence(sequence)
    # Assert
    assert sent == ["2", "Enter"]


def test_priority_wins_when_multiple_prompts_match():
    """If a pane somehow shows two detectable prompts at once, the
    lowest-priority-number handler wins. bypass-permissions (pri=1)
    beats dev-channels (pri=2)."""
    # Arrange
    combined = (
        "Bypass Permissions\n2. Yes, I accept\n"
        "1. I am using this for local development\n2. Exit\n"
        "Enter to confirm"
    )
    # Act
    result = detect_and_respond(combined, set(), lambda k: None)
    # Assert
    assert result == "bypass-permissions"


# ── detect() + respond_modal() — the verify-retry primitives ──────────────────


def test_detect_returns_handler_name_for_known_modal():
    # Arrange
    pane = "1. I am using this for local development\n2. Exit\nEnter to confirm"
    # Act
    name = detect(pane)
    # Assert
    assert name == "dev-channels"


def test_detect_returns_none_for_quiet_pane():
    # Arrange
    pane = "history line\n❯\n"
    # Act
    name = detect(pane)
    # Assert
    assert name is None


def test_detect_returns_compose_pending_for_nbsp_paste_buffer():
    # Arrange — Claude's Ink TUI renders the prompt gap as U+00A0 NBSP, so a
    # multi-line startup_prompt paste shows as ``❯\xa0[Pasted text …]``. The
    # detector MUST classify it (the bug that left proj-scitex-dev's prompt
    # pasted-but-unsent: ``❯[ \t]+`` missed the NBSP → no Enter resend).
    pane = "❯\xa0[Pasted text #1 +26 lines]\n"
    # Act
    name = detect(pane)
    # Assert
    assert name == "compose-pending-unsent"


def test_respond_modal_sends_the_registered_keys():
    # Arrange
    sent: list[str] = []
    # Act
    respond_modal("dev-channels", lambda k: sent.append(k))
    # Assert
    assert sent == ["1", "Enter"]


def test_respond_modal_returns_false_for_unknown_name():
    # Arrange
    sent: list[str] = []
    # Act
    handled = respond_modal("no-such-modal", lambda k: sent.append(k))
    # Assert
    assert handled is False


@pytest.mark.parametrize(
    "content",
    [
        "Bypass Permissions\n2. Yes, I accept\nEnter to confirm",
        "1. I am using this for local development\n2. Exit\nEnter to confirm",
    ],
)
def test_is_ready_false_while_prompt_visible(content):
    """is_ready() must stay False while any TUI prompt is still
    visible — otherwise the startup-commands flush races the auto-
    accept and gets stolen by the radio selector."""
    # Arrange
    pane = content
    # Act
    result = is_ready(pane)
    # Assert
    assert result is False


def test_is_ready_true_when_status_bar_settled():
    """The status bar shows 'bypass permissions' once all prompts
    clear — that's the canonical ready signal."""
    # Arrange
    pane = "> \nbypass permissions active\n"
    # Act
    result = is_ready(pane)
    # Assert
    assert result is True


@pytest.mark.parametrize(
    "pane",
    ["", "some chatter\nnothing yet\n"],
)
def test_is_ready_false_if_status_bar_absent(pane):
    """An empty pane or a pane without the status bar string is not ready."""
    # Arrange
    content = pane
    # Act
    result = is_ready(content)
    # Assert
    assert result is False


def test_register_prompt_pre_bypass_returns_custom_handler_name():
    """Plugin extension point: register_prompt() inserts and re-sorts
    so detect_and_respond still walks in priority order. A
    pri=0 custom handler beats the stock bypass-permissions (pri=1)."""
    # Arrange
    snapshot = list(PROMPT_HANDLERS)
    hit: list[str] = []
    try:
        register_prompt(
            PromptHandler(
                name="custom-pre-bypass",
                detect=lambda c: "Bypass Permissions" in c,
                keys=["custom-key"],
                priority=0,
            )
        )
        content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
        # Act
        result = detect_and_respond(content, set(), lambda k: hit.append(k))
        # Assert
        assert result == "custom-pre-bypass"
    finally:
        # Restore module-level state so later tests see the stock list.
        PROMPT_HANDLERS[:] = snapshot


def test_register_prompt_pre_bypass_sends_custom_handler_keys():
    # Arrange
    snapshot = list(PROMPT_HANDLERS)
    hit: list[str] = []
    try:
        register_prompt(
            PromptHandler(
                name="custom-pre-bypass-keys",
                detect=lambda c: "Bypass Permissions" in c,
                keys=["custom-key"],
                priority=0,
            )
        )
        content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
        # Act
        detect_and_respond(content, set(), lambda k: hit.append(k))
        # Assert
        assert hit == ["custom-key"]
    finally:
        PROMPT_HANDLERS[:] = snapshot


def test_no_handler_returns_none_on_quiet_pane():
    """Handlers fire only on positive detection; quiet pane -> None."""
    # Arrange
    sent: list[str] = []
    # Act
    result = detect_and_respond("\n> \n", set(), lambda k: sent.append(k))
    # Assert
    assert result is None


def test_no_handler_sends_no_keys_on_quiet_pane():
    """Handlers fire only on positive detection; quiet pane -> no keys."""
    # Arrange
    sent: list[str] = []
    # Act
    detect_and_respond("\n> \n", set(), lambda k: sent.append(k))
    # Assert
    assert sent == []
