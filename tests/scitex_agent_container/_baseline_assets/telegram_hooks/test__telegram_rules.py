"""Unit tests for the `#NNN`-needs-a-description predicate itself.

This is the mirror-convention companion to
`src/scitex_agent_container/_baseline_assets/telegram_hooks/_telegram_rules.py`,
and it is a genuinely different layer from
`tests/integration/telegram_hooks/test_telegram_rule_ssot.py`. That file
drives the two ADAPTERS as subprocesses and asserts they cannot disagree;
this one calls `check_message` in process. Two things live here that no
adapter-level test can pin:

  * THE DECISIONS THEMSELVES, asserted against the one function that
    makes them. When the shell hook and the MCP filter agree, the SSoT
    test is green whatever they agree ON — including agreeing on a wrong
    answer. This file says what the right answer is.
  * THE API THE ADAPTERS CONSUME — the `Verdict` shape, the `as_dict`
    payload the MCP binding serialises, the `ESCAPE_ENV` name both
    honour, and the fact that the refusal WORDING is produced here
    rather than by any caller.

Loaded by path, not imported: `_baseline_assets/telegram_hooks/` ships as
package DATA (the files are materialized into an agent's
`$HOME/.claude/hooks/pre-tool-use/`), so it deliberately is not
importable as a module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# tests/scitex_agent_container/_baseline_assets/telegram_hooks/<this file>
# -> parents[4] is the repo root. Anchored on __file__ so the test reads
# the tree it was collected from, never an installed copy.
_RULES_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
    / "_telegram_rules.py"
)


def _load_rules():
    spec = importlib.util.spec_from_file_location("_telegram_rules", _RULES_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_telegram_rules"] = module
    spec.loader.exec_module(module)
    return module


_RULES = _load_rules()

REJECT = False
ACCEPT = True


@pytest.fixture
def escape_env():
    """Set the real override variable in the real environment.

    `check_message` reads `os.environ` at call time, so this hands the
    test a setter for the actual variable and restores whatever was
    there on teardown — nothing here rewrites the predicate's internals.
    """
    name = _RULES.ESCAPE_ENV
    previous = os.environ.get(name)

    def _set(value):
        os.environ[name] = value

    yield _set

    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.fixture(autouse=True)
def _no_ambient_override():
    """A machine that exports the override must not silently turn every
    REJECT case in this file into a false green."""
    name = _RULES.ESCAPE_ENV
    previous = os.environ.pop(name, None)

    yield

    if previous is not None:
        os.environ[name] = previous


# The operator's decided behaviour, asserted against the predicate that
# decides it. Every entry traces to something he said, or to a false
# positive that would have got the whole gate switched off.
_DECIDED = [
    # --- what he refuses: a number he cannot read ---------------------
    ("bare_number", "#589", REJECT),
    ("repo_name_is_not_a_description", "scitex-dev #589", REJECT),
    ("label_is_not_a_description", "PR #589", REJECT),
    ("owner_repo_bare", "ywatanabe1989/scitex-dev#578", REJECT),
    ("mid_sentence", "調べていて見つかった #578、これはまだ直っていません", REJECT),
    # --- what he asked for: the PARENTHESIS ---------------------------
    #     「ナンバーの後に ( をつけて説明する、っていうのをルールにして
    #       ください」
    ("parenthetical_ascii", "#589 (auditd rules declared)", ACCEPT),
    ("parenthetical_fullwidth", "#589（auditd ルールを宣言）", ACCEPT),
    ("parenthetical_no_space", "#589(auditd rules declared)", ACCEPT),
    ("empty_parenthesis_describes_nothing", "#589 ()", REJECT),
    # --- and NOT some other punctuation he did not ask for ------------
    ("em_dash_form", "#589 — auditd rules declared", REJECT),
    ("hyphen_form", "#589 - auditd rules declared", REJECT),
    ("colon_form", "#589: auditd rules declared", REJECT),
    # --- false positives: a gate that fires here gets switched off ----
    ("hex_colour", "#589abc", ACCEPT),
    ("hex_colour_in_prose", "use #589abc for the border", ACCEPT),
    ("url_path_segment", "https://github.com/o/r/pull/589", ACCEPT),
    ("url_fragment", "https://github.com/o/r/pull/589#issuecomment-123", ACCEPT),
    ("inline_code_span", "the token `#589` is data here", ACCEPT),
    ("fenced_code_block", "see below\n```\n#589\n```", ACCEPT),
    ("markdown_heading", "# 970 title", ACCEPT),
    ("html_numeric_entity", "a dash &#8212; here", ACCEPT),
    # --- neither a link nor code may SUPPLY the parenthesis -----------
    ("url_cannot_bridge_to_paren", "#970 https://e.com/x (説明)", REJECT),
    ("code_cannot_bridge_to_paren", "#970 `x` (説明)", REJECT),
    # --- a repeat inherits the description, strictly LEFT-TO-RIGHT ----
    #     He reads top to bottom, so a bare mention BEFORE its
    #     description is still a number he cannot read.
    ("repeat_described_then_bare", "#970（修正）を出した。#970 のCIは緑", ACCEPT),
    ("repeat_bare_then_described", "#970 のCIは緑。#970（修正）", REJECT),
    ("two_numbers_one_described", "#970（修正）と #971", REJECT),
    ("two_numbers_both_described", "#970（修正）と #971（テスト追加）", ACCEPT),
    # --- nothing to enforce -------------------------------------------
    ("no_reference_at_all", "CI is green, nothing blocking", ACCEPT),
    ("empty_text", "", ACCEPT),
]

_DECIDED_IDS = [case[0] for case in _DECIDED]


@pytest.mark.parametrize("case_id, text, expected", _DECIDED, ids=_DECIDED_IDS)
def test_the_predicate_decides_the_operators_table(case_id, text, expected):
    """The answers themselves, not merely that two adapters share them."""
    # Arrange
    message = text

    # Act
    verdict = _RULES.check_message(message)

    # Assert
    assert verdict.ok is expected


def test_a_cjk_suffixed_reference_is_still_refused():
    """Why the hex-colour lookahead is ASCII-only and not ``\\w``.

    Python's ``\\w`` matches CJK, so a ``\\w`` guard written to exempt
    ``#589abc`` would ALSO swallow this — a real bare reference written
    without a space, and the exact message the operator complained about
    on 2026-08-11. Change ``[0-9A-Za-z]`` to ``\\w`` in the predicate and
    this test goes red; nothing else in the suite notices.
    """
    # Arrange
    text = "#970の話ではなく"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert verdict.ok is False


def test_a_hex_colour_and_a_cjk_reference_are_told_apart():
    """The pair the ASCII-only lookahead exists to separate."""
    # Arrange
    colour, reference = "#589abc", "#589の話"

    # Act
    colour_ok = _RULES.check_message(colour).ok
    reference_ok = _RULES.check_message(reference).ok

    # Assert
    assert (colour_ok, reference_ok) == (True, False)


def test_the_refused_verdict_carries_the_offending_token():
    # Arrange
    text = "調べていて見つかった #578、これはまだ直っていません"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert verdict.token == "#578"


def test_the_refused_verdict_quotes_the_offending_excerpt():
    """He must see WHICH part of his message tripped the rule."""
    # Arrange
    text = "scitex-dev #589"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert "scitex-dev #589" in verdict.excerpt


def test_the_predicate_owns_the_refusal_wording():
    """No caller composes its own — that is the whole point of the SSoT."""
    # Arrange
    text = "scitex-dev #589"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert "#589 (auditd rules declared)" in verdict.message


def test_the_refusal_states_that_the_dash_form_does_not_pass():
    """The refusal is the fix instruction; it must name the form he wants."""
    # Arrange
    text = "#589 - auditd rules declared"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert "a dash is not the form" in verdict.message


def test_the_refusal_names_the_override_env():
    # Arrange
    text = "#589"

    # Act
    verdict = _RULES.check_message(text)

    # Assert
    assert _RULES.ESCAPE_ENV in verdict.message


def test_as_dict_is_minimal_when_accepted():
    """The MCP binding serialises this; an accept carries no noise."""
    # Arrange
    text = "#589 (auditd rules declared)"

    # Act
    payload = _RULES.check_message(text).as_dict()

    # Assert
    assert payload == {"ok": True}


def test_as_dict_carries_the_full_payload_when_refused():
    # Arrange
    text = "scitex-dev #589"

    # Act
    payload = _RULES.check_message(text).as_dict()

    # Assert
    assert set(payload) == {"ok", "token", "excerpt", "message"}


@pytest.mark.parametrize(
    "value",
    [None, 123, b"#589", []],
    ids=["none", "int", "bytes", "list"],
)
def test_fails_open_on_a_surprising_payload_shape(value):
    """A surprising shape is never reported as a rule violation."""
    # Arrange
    payload = value

    # Act
    verdict = _RULES.check_message(payload)

    # Assert
    assert verdict.ok is True


def test_the_escape_env_name_is_exported_for_both_adapters():
    # Arrange
    module = _RULES

    # Act
    name = module.ESCAPE_ENV

    # Assert
    assert name == "CC_ALLOW_BARE_ISSUE"


def test_the_escape_env_short_circuits_the_predicate(escape_env):
    """The override is honoured HERE, so no adapter has to remember it."""
    # Arrange
    escape_env("1")

    # Act
    verdict = _RULES.check_message("#589")

    # Assert
    assert verdict.ok is True


def test_the_escape_env_is_off_unless_it_is_exactly_one(escape_env):
    """An unset-looking value must not disarm the gate by accident."""
    # Arrange
    escape_env("0")

    # Act
    verdict = _RULES.check_message("#589")

    # Assert
    assert verdict.ok is False
