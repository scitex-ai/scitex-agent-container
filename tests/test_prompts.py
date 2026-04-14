"""Tests for prompts.py — TUI prompt detection handlers."""
import pytest
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
    result = detect_and_respond(content, {"bypass-permissions"}, lambda k: sent.append(k))
    assert result is None
    assert sent == []
