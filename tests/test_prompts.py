"""Tests for prompts.py — TUI prompt detection handlers."""


from scitex_agent_container.runtimes.prompts import (
    PROMPT_HANDLERS,
    _detect_bypass_permissions,
    _detect_dev_channels,
    _detect_file_trust,
    _detect_mcp_json_edit,
    _detect_press_enter_continue,
    _detect_skip_permissions_yn,
    _detect_thinking_effort,
    detect_and_respond,
)

# ── Startup handlers ──────────────────────────────────────────────────────────


def test_bypass_permissions_match():
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    assert _detect_bypass_permissions(content) is True


def test_bypass_permissions_no_match():
    assert _detect_bypass_permissions("Normal chat text") is False


def test_dev_channels_match():
    content = "1. I am using this for local development\n2. Exit\nEnter to confirm"
    assert _detect_dev_channels(content) is True


def test_thinking_effort_match():
    content = "1. * Medium (recommended)\nthinking effort\nEnter to confirm"
    assert _detect_thinking_effort(content) is True


def test_skip_permissions_yn_match():
    content = "skip-permissions y/n"
    assert _detect_skip_permissions_yn(content) is True


# ── Runtime handlers ──────────────────────────────────────────────────────────


def test_mcp_json_edit_match():
    content = ".mcp.json\n1. Yes, proceed\nEnter to confirm"
    assert _detect_mcp_json_edit(content) is True


def test_mcp_json_edit_no_match_without_confirm():
    content = ".mcp.json updated successfully"
    assert _detect_mcp_json_edit(content) is False


def test_press_enter_continue_match():
    content = "Context window approaching limit.\nPress Enter to continue"
    assert _detect_press_enter_continue(content) is True


def test_press_enter_continue_no_match_active():
    content = "Working\u2026 Press Enter to continue"
    assert _detect_press_enter_continue(content) is False


def test_press_enter_continue_no_match_radio():
    content = "1. Option A\n2. Option B\nEnter to confirm\nPress Enter to continue"
    assert _detect_press_enter_continue(content) is False


def test_press_enter_continue_only_checks_tail():
    # Old scrollback + current idle prompt should NOT trigger
    # (strict last-5-lines window: the last visible line is the shell prompt)
    old = "Press Enter to continue\n" * 50
    current = old + "Some output\nMore output\nAnd more\nAnd even more\n\u276f "
    assert _detect_press_enter_continue(current) is False


def test_file_trust_match():
    content = "Do you trust the files in this folder? yes/no"
    assert _detect_file_trust(content) is True


def test_file_trust_no_match_no_folder():
    assert _detect_file_trust("Do you trust this?") is False


# ── Registry ─────────────────────────────────────────────────────────────────


def test_all_handlers_present():
    names = {h.name for h in PROMPT_HANDLERS}
    expected = {
        "bypass-permissions",
        "dev-channels",
        "thinking-effort",
        "mcp-json-edit",
        "skip-permissions-yn",
        "press-enter-continue",
        "file-trust",
    }
    assert expected <= names


def test_handlers_sorted_by_priority():
    priorities = [h.priority for h in PROMPT_HANDLERS]
    assert priorities == sorted(priorities)


def test_detect_and_respond_calls_send_keys():
    sent = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    result = detect_and_respond(content, set(), lambda k: sent.append(k))
    assert result == "bypass-permissions"
    assert "2" in sent
    assert "Enter" in sent


def test_detect_and_respond_skips_accepted():
    sent = []
    content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    result = detect_and_respond(
        content, {"bypass-permissions"}, lambda k: sent.append(k)
    )
    assert result is None
    assert sent == []


# ── Timing / sequencing scenarios ─────────────────────────────────────────────
# The user's real-world pain point is auto-accept through a multi-prompt
# TUI boot sequence where prompts arrive one after another on a noisy pane
# that the auto-accept loop is polling. These tests simulate the full flow
# without needing a real tmux pane.

from scitex_agent_container.runtimes.prompts import (  # noqa: E402
    PromptHandler,
    is_ready,
    register_prompt,
)


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


def test_realistic_boot_sequence_each_prompt_accepted_once():
    """Simulates a Claude Code boot that walks through three prompts
    (bypass-permissions -> dev-channels -> thinking-effort), then
    settles at the main prompt. Each handler fires exactly once, in
    priority order, and the final ready pane triggers nothing."""
    bypass = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    dev = "1. I am using this for local development\n2. Exit\nEnter to confirm"
    thinking = "1. Medium (recommended)\nthinking effort\nEnter to confirm"
    ready = "> \nbypass permissions active\n"
    matches, accepted, sent = _simulate_sequence([bypass, dev, thinking, ready])
    assert matches == [
        "bypass-permissions",
        "dev-channels",
        "thinking-effort",
        None,
    ]
    assert accepted == {"bypass-permissions", "dev-channels", "thinking-effort"}
    # Each handler's keys were sent in order.
    assert sent == ["2", "Enter", "1", "Enter", "1", "Enter"]


def test_redraw_of_same_prompt_is_not_re_accepted():
    """A slow pane may redraw the same prompt across several polls
    before the keystrokes are processed. Once we've accepted a
    prompt, re-seeing it must be a no-op — double-accept would
    press keys into the ready prompt and corrupt the next message."""
    bypass = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    matches, accepted, sent = _simulate_sequence([bypass, bypass, bypass])
    assert matches == ["bypass-permissions", None, None]
    assert sent == ["2", "Enter"]  # Exactly one accept.


def test_priority_wins_when_multiple_prompts_match():
    """If a pane somehow shows two detectable prompts at once, the
    lowest-priority-number handler wins. bypass-permissions (pri=1)
    beats dev-channels (pri=2)."""
    combined = (
        "Bypass Permissions\n2. Yes, I accept\n"
        "1. I am using this for local development\n2. Exit\n"
        "Enter to confirm"
    )
    result = detect_and_respond(combined, set(), lambda k: None)
    assert result == "bypass-permissions"


def test_is_ready_false_during_prompt_walk():
    """is_ready() must stay False while any TUI prompt is still
    visible — otherwise the startup-commands flush races the auto-
    accept and gets stolen by the radio selector. Confirmed: the
    status bar only shows 'bypass permissions' once all prompts
    clear, and that's the canonical ready signal."""
    bypass = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
    dev = "1. I am using this for local development\n2. Exit\nEnter to confirm"
    # Prompt visible -> not ready even if the status bar string is
    # also present somewhere in the buffer.
    assert is_ready(bypass) is False
    assert is_ready(dev) is False
    # Main prompt settled -> ready.
    assert is_ready("> \nbypass permissions active\n") is True


def test_is_ready_false_if_status_bar_absent():
    """An empty pane is not ready (no status bar string)."""
    assert is_ready("") is False
    assert is_ready("some chatter\nnothing yet\n") is False


def test_register_prompt_respects_priority_ordering():
    """Plugin extension point: register_prompt() inserts and re-sorts
    so detect_and_respond still walks in priority order. A
    pri=0 custom handler beats the stock bypass-permissions (pri=1)."""
    snapshot = list(PROMPT_HANDLERS)
    try:
        hit = []
        register_prompt(
            PromptHandler(
                name="custom-pre-bypass",
                detect=lambda c: "Bypass Permissions" in c,
                keys=["custom-key"],
                priority=0,
            )
        )
        content = "Bypass Permissions\n2. Yes, I accept\nEnter to confirm"
        result = detect_and_respond(content, set(), lambda k: hit.append(k))
        assert result == "custom-pre-bypass"
        assert hit == ["custom-key"]
    finally:
        # Restore module-level state so later tests see the stock list.
        PROMPT_HANDLERS[:] = snapshot


def test_no_handler_returns_none_without_touching_keys():
    """Handlers fire only on positive detection; quiet pane -> nothing."""
    sent = []
    result = detect_and_respond("\n> \n", set(), lambda k: sent.append(k))
    assert result is None
    assert sent == []
