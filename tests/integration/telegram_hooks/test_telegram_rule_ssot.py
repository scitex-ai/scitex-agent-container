"""ONE rule, two adapters — and the test that proves they cannot disagree.

The operator's design constraint, 2026-08-12:

    「mcp も同じですね。同じルールなので、ルールは一つの場所に、shell 用の
      hook と mcp のフィルタで同じルールを適用させて ssot に、が良いかと」

Two consumers — the Claude Code PreToolUse shell hook, and the MCP-side
filter inside claude-code-telegrammer — must apply the SAME rule. The
failure being designed against is concrete: a rule enforced on one path
and absent on the other means a message blocked one way sails through
the other, which is exactly how a bare ``scitex-dev #589`` reached his
phone on 2026-08-11.

WHY THIS FILE DRIVES BOTH PATHS OVER ONE TABLE. That the two adapters
call the same function is an implementation detail, and an
implementation detail is not the property. The PROPERTY is that the two
paths return the same verdict and the same wording for the same input,
so this asserts it end to end — the shell adapter as a subprocess over
Claude Code hook JSON, the MCP adapter as a subprocess over the
language-agnostic ``--text-stdin`` contract the TypeScript server calls.
A regression that reintroduced a second copy of the rule would still
pass a test that only called ``check_message`` twice; it fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
)
_HOOK = _HOOKS_DIR / "enforce_telegram_no_bare_issue.sh"
_RULES = _HOOKS_DIR / "_telegram_rules.py"

_REPLY_TOOL = "mcp__claude-code-telegrammer__reply"

REJECT = "REJECT"
ACCEPT = "accept"

# (case_id, message_text, expected_verdict)
#
# Every case the operator named, plus every false positive that would
# get the rule switched off if it fired. One table, both adapters.
_CASES: list[tuple[str, str, str]] = [
    # --- the operator's explicit REJECT list -------------------------
    ("bare_number", "#589", REJECT),
    ("repo_name_is_not_a_description", "scitex-dev #589", REJECT),
    ("label_is_not_a_description", "PR #589", REJECT),
    ("owner_repo_bare", "ywatanabe1989/scitex-dev#578", REJECT),
    ("mid_sentence", "調べていて見つかった #578、これはまだ直っていません", REJECT),
    ("no_space_before_cjk", "#970の話ではなく", REJECT),
    # --- the operator's explicit ACCEPT: the parenthetical form ------
    ("parenthetical_ascii", "#589 (auditd rules declared)", ACCEPT),
    ("parenthetical_fullwidth", "#589（auditd ルールを宣言）", ACCEPT),
    ("parenthetical_no_space", "#589(auditd rules declared)", ACCEPT),
    # --- the DASH form is REFUSED: he stipulated the parenthesis -----
    #     「ナンバーの後に ( をつけて説明する、っていうのをルールにして
    #       ください」 — a dash is a different form, not the one he asked
    #     for, and the refusal text says so explicitly.
    ("em_dash_form_is_refused", "#589 — auditd rules declared", REJECT),
    ("hyphen_dash_form_is_refused", "#589 - auditd rules declared", REJECT),
    ("colon_form_is_refused", "#589: auditd rules declared", REJECT),
    # --- false positives: a gate that fires here gets switched off ---
    ("hex_colour_6", "#589abc", ACCEPT),
    ("hex_colour_3", "#fff", ACCEPT),
    ("hex_colour_upper", "#58ABCD", ACCEPT),
    ("hex_colour_in_prose", "use #589abc for the border", ACCEPT),
    ("url_fragment", "https://github.com/o/r/pull/589#issuecomment-123", ACCEPT),
    ("url_path_segment", "https://github.com/o/r/pull/589", ACCEPT),
    ("url_numeric_fragment", "見てください https://example.com/page#123", ACCEPT),
    ("inline_code_span", "the token `#589` is data here", ACCEPT),
    ("fenced_code_block", "see below\n```\n#589\n```", ACCEPT),
    ("markdown_heading", "# Title", ACCEPT),
    ("markdown_heading_h2", "## 5 things", ACCEPT),
    ("html_numeric_entity", "a dash &#8212; here", ACCEPT),
    # --- neither a link nor code may SUPPLY the parenthesis ----------
    ("url_cannot_bridge_to_paren", "#970 https://e.com/x (説明)", REJECT),
    ("code_cannot_bridge_to_paren", "#970 `x` (説明)", REJECT),
    # --- the repeated-reference allowance, strictly left-to-right ----
    ("repeat_described_then_bare", "#970（修正）を出した。#970 のCIは緑", ACCEPT),
    ("repeat_bare_then_described", "#970 のCIは緑。#970（修正）", REJECT),
    ("two_numbers_one_described", "#970（修正）と #971", REJECT),
    ("two_numbers_both_described", "#970（修正）と #971（テスト追加）", ACCEPT),
    # --- nothing to enforce ------------------------------------------
    ("no_reference_at_all", "CI is green, nothing blocking", ACCEPT),
    ("empty_text", "", ACCEPT),
]

_IDS = [case[0] for case in _CASES]
_REJECT_CASES = [case for case in _CASES if case[2] == REJECT]
_REJECT_IDS = [case[0] for case in _REJECT_CASES]


def _shell_hook(text, extra_env=None):
    """Adapter 1 — the Claude Code PreToolUse shell hook, as a subprocess."""
    env = dict(os.environ)
    env.pop("CC_ALLOW_BARE_ISSUE", None)
    if extra_env:
        env.update(extra_env)
    payload = json.dumps(
        {"tool_name": _REPLY_TOOL, "tool_input": {"chat_id": "1", "text": text}}
    )
    proc = subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    verdict = REJECT if proc.returncode == 2 else ACCEPT
    return verdict, proc.stderr


def _mcp_filter(text, extra_env=None):
    """Adapter 2 — the language-agnostic contract the MCP filter calls."""
    env = dict(os.environ)
    env.pop("CC_ALLOW_BARE_ISSUE", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(_RULES), "--text-stdin"],
        input=text,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(proc.stdout)
    verdict = ACCEPT if payload["ok"] else REJECT
    return verdict, payload.get("message", "")


@pytest.mark.parametrize("case_id, text, expected", _CASES, ids=_IDS)
def test_shell_hook_adapter_matches_the_table(case_id, text, expected):
    # Arrange
    message = text

    # Act
    verdict, _ = _shell_hook(message)

    # Assert
    assert verdict == expected


@pytest.mark.parametrize("case_id, text, expected", _CASES, ids=_IDS)
def test_mcp_filter_adapter_matches_the_table(case_id, text, expected):
    # Arrange
    message = text

    # Act
    verdict, _ = _mcp_filter(message)

    # Assert
    assert verdict == expected


@pytest.mark.parametrize("case_id, text, expected", _CASES, ids=_IDS)
def test_both_adapters_return_the_same_verdict(case_id, text, expected):
    """The property the SSoT exists for, asserted directly."""
    # Arrange
    message = text

    # Act
    shell_verdict, _ = _shell_hook(message)
    mcp_verdict, _ = _mcp_filter(message)

    # Assert
    assert shell_verdict == mcp_verdict


@pytest.mark.parametrize("case_id, text, expected", _REJECT_CASES, ids=_REJECT_IDS)
def test_both_adapters_return_the_same_wording(case_id, text, expected):
    """The wording IS the fix instruction; two paths must not drift on it.

    The shell hook writes the refusal to stderr; the MCP filter returns
    it as ``message``. Neither composes its own — both render what
    ``_telegram_rules`` produced, so the text must be byte-identical.
    """
    # Arrange
    message = text

    # Act
    _, shell_text = _shell_hook(message)
    _, mcp_text = _mcp_filter(message)

    # Assert
    assert shell_text.strip() == mcp_text.strip()


@pytest.mark.parametrize("case_id, text, expected", _REJECT_CASES, ids=_REJECT_IDS)
def test_every_refusal_names_the_override_env(case_id, text, expected):
    # Arrange
    message = text

    # Act
    _, shell_text = _shell_hook(message)

    # Assert
    assert "Rare one-off override: set env CC_ALLOW_BARE_ISSUE=1." in shell_text


def test_refusal_shows_the_accepted_parenthetical_form():
    """A refusal must make the fix obvious."""
    # Arrange
    offending = "scitex-dev #589"

    # Act
    _, message = _mcp_filter(offending)

    # Assert
    assert "#589 (auditd rules declared)" in message


def test_refusal_states_that_the_dash_form_does_not_pass():
    """Whatever was decided, the refusal must say which forms fail."""
    # Arrange
    offending = "scitex-dev #589"

    # Act
    _, message = _mcp_filter(offending)

    # Assert
    assert "a dash is not the form" in message


def test_refusal_quotes_the_offending_excerpt():
    """He must see WHICH part of his message tripped the rule."""
    # Arrange
    offending = "scitex-dev #589"

    # Act
    _, message = _mcp_filter(offending)

    # Assert
    assert "scitex-dev #589" in message


def test_escape_env_is_honoured_by_the_shell_hook():
    # Arrange
    offending = "#589"

    # Act
    verdict, _ = _shell_hook(offending, extra_env={"CC_ALLOW_BARE_ISSUE": "1"})

    # Assert
    assert verdict == ACCEPT


def test_escape_env_is_honoured_by_the_mcp_filter():
    # Arrange
    offending = "#589"

    # Act
    verdict, _ = _mcp_filter(offending, extra_env={"CC_ALLOW_BARE_ISSUE": "1"})

    # Assert
    assert verdict == ACCEPT


def test_the_rule_module_ships_beside_the_hook():
    """The hook FAILS OPEN when the module is missing, so CI must pin
    that they travel together — otherwise a packaging slip would
    silently disarm the gate rather than break loudly."""
    # Arrange
    expected_module = _RULES

    # Act
    exists = expected_module.is_file()

    # Assert
    assert exists is True


def test_the_shell_hook_points_at_the_rule_module():
    """A second copy of the rule is the failure being designed against."""
    # Arrange
    source = _HOOK.read_text(encoding="utf-8")

    # Act
    references_module = "_telegram_rules.py" in source

    # Assert
    assert references_module is True


def test_the_shell_hook_does_not_reimplement_the_reference_regex():
    """If the hook ever grows its own matcher, the two paths can drift —
    which is the entire failure this extraction exists to prevent."""
    # Arrange
    source = _HOOK.read_text(encoding="utf-8")

    # Act
    reimplements = r"#(\d+)" in source

    # Assert
    assert reimplements is False
