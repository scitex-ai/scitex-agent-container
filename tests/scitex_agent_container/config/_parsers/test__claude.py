"""Tests for ``config._parsers._claude.parse_claude``.

Each test pins exactly one observable behaviour of the parser. The
``spec.claude`` block is optional and collapses to a ``ClaudeSpec`` with
documented defaults when missing or explicitly ``None``. The top-level
``session`` key takes precedence over the nested ``claude.session`` for
ergonomics, while legacy aliases are normalised (``continue-or-new`` →
``continue``; ``new`` → ``new-session``). The ``continue_max_age_minutes``
field is coerced to ``int`` when possible and silently drops to ``None``
on malformed input. The ``raw_options`` field passes dict payloads
through unchanged and replaces non-dict values with an empty dict.
Remaining fields (``model``/``channels``/``flags``/``resume_id``/
``auto_accept``) are surfaced verbatim from the nested block.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._claude import parse_claude

# ---------------------------------------------------------------------------
# Missing claude block → default ClaudeSpec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("model", ""),
        ("channels", []),
        ("flags", []),
        ("session", "continue"),
        ("continue_max_age_minutes", None),
        ("resume_id", ""),
        ("auto_accept", True),
        ("account", ""),
        ("raw_options", {}),
    ],
)
def test_missing_claude_block_yields_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_claude(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Explicit None claude block → treated as empty dict (defaults restored)
# ---------------------------------------------------------------------------


def test_explicit_none_claude_block_yields_default_session():
    # Arrange
    spec = {"claude": None}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.session == "continue"


# ---------------------------------------------------------------------------
# Session precedence and legacy alias normalisation
# ---------------------------------------------------------------------------


def test_top_level_session_overrides_nested_session_with_alias_applied():
    # Arrange: legacy `new` is normalised to `new-session` per
    # REQUIREMENT_SUMMARY §3 #6, and top-level wins over nested.
    spec = {"session": "new", "claude": {"session": "continue"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.session == "new-session"


def test_nested_continue_or_new_alias_normalises_to_continue():
    # Arrange: `continue-or-new` is the safe-fallback alias for `continue`.
    spec = {"claude": {"session": "continue-or-new"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.session == "continue"


def test_nested_session_used_when_top_level_absent():
    # Arrange
    spec = {"claude": {"session": "continue"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.session == "continue"


# ---------------------------------------------------------------------------
# continue_max_age_minutes coercion
# ---------------------------------------------------------------------------


def test_continue_max_age_minutes_numeric_string_is_coerced_to_int():
    # Arrange
    spec = {"claude": {"continue_max_age_minutes": "30"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.continue_max_age_minutes == 30


def test_continue_max_age_minutes_unparsable_string_becomes_none():
    # Arrange
    spec = {"claude": {"continue_max_age_minutes": "abc"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.continue_max_age_minutes is None


# ---------------------------------------------------------------------------
# raw_options pass-through and non-dict guard
# ---------------------------------------------------------------------------


def test_raw_options_dict_is_passed_through_unchanged():
    # Arrange
    spec = {"claude": {"raw_options": {"k": "v"}}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.raw_options == {"k": "v"}


def test_raw_options_non_dict_value_yields_empty_dict():
    # Arrange
    spec = {"claude": {"raw_options": "garbage"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.raw_options == {}


# ---------------------------------------------------------------------------
# Direct field pass-through (model / channels / flags / resume_id / auto_accept)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("model", "opus[1m]"),
        ("channels", ["beta"]),
        ("flags", ["--foo"]),
        ("resume_id", "abc-123"),
        ("auto_accept", False),
    ],
)
def test_nested_field_is_surfaced_verbatim(attr, expected):
    # Arrange
    spec = {
        "claude": {
            "model": "opus[1m]",
            "channels": ["beta"],
            "flags": ["--foo"],
            "resume_id": "abc-123",
            "auto_accept": False,
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# spec.claude.account — per-agent OAuth account pin
# ---------------------------------------------------------------------------


def test_account_parsed_from_nested_block():
    # Arrange
    spec = {"claude": {"account": "work"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.account == "work"


def test_account_none_coerced_to_empty_string():
    # Arrange — an explicit null must collapse to the host-live-file default.
    spec = {"claude": {"account": None}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.account == ""


# ---------------------------------------------------------------------------
# spec.claude.provider — vendor-agnostic backend override
# ---------------------------------------------------------------------------


def test_missing_provider_block_yields_none_provider():
    # Arrange — no provider key means the default Anthropic backend.
    spec = {"claude": {"model": "opus"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider is None


def test_provider_block_parses_base_url_field():
    # Arrange
    spec = {
        "claude": {
            "provider": {
                "base_url": "https://api.deepseek.com/anthropic",
                "auth_token_env": "DEEPSEEK_API_KEY",
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.base_url == "https://api.deepseek.com/anthropic"


def test_provider_block_parses_auth_token_env_field():
    # Arrange
    spec = {
        "claude": {
            "provider": {
                "base_url": "https://api.deepseek.com/anthropic",
                "auth_token_env": "DEEPSEEK_API_KEY",
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.auth_token_env == "DEEPSEEK_API_KEY"


def test_provider_non_dict_value_yields_none_provider():
    # Arrange — an unregistered (non-mapping, non-registered-name)
    # provider collapses to None so the validator surfaces the shape
    # error, not the parser.
    spec = {"claude": {"provider": "garbage"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider is None


# ---------------------------------------------------------------------------
# String-form provider (ADR-0011 extension, operator directive 2026-05-28)
# ---------------------------------------------------------------------------


def test_provider_string_form_mimo_resolves_to_xiaomi_base_url():
    # Arrange
    spec = {"claude": {"provider": "mimo"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.base_url == "https://token-plan-sgp.xiaomimimo.com/anthropic"


def test_provider_string_form_mimo_resolves_to_xiaomi_auth_env():
    # Arrange
    spec = {"claude": {"provider": "mimo"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.auth_token_env == "XIAOMI_API_KEY"


def test_provider_string_form_deepseek_resolves_to_deepseek_base_url():
    # Arrange
    spec = {"claude": {"provider": "deepseek"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.base_url == "https://api.deepseek.com/anthropic"


def test_provider_string_form_anthropic_sentinel_yields_no_override():
    # Arrange — registered name with base_url=None means
    # "use the default Anthropic OAuth backend, no override".
    spec = {"claude": {"provider": "anthropic"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider is None


# ---------------------------------------------------------------------------
# PR #319 (lead msg a456b610 2026-06-06): provider.allowed_tools whitelist.
# Operator declares which built-in tools their shim recognizes; the runner
# uses that to populate ClaudeAgentOptions.tools so unrecognized newer
# Claude Code builtins (ExitPlanMode, BashOutput, KillShell) never enter
# the outbound API request body. Root cause: LiteLLM 1.52.16's pydantic
# Union mis-routes unrecognized tools → AnthropicComputerTool → 422.
# ---------------------------------------------------------------------------


def test_provider_dict_form_parses_allowed_tools_list():
    # Arrange
    spec = {
        "claude": {
            "provider": {
                "base_url": "http://127.0.0.1:4000",
                "auth_token_env": "VLLM_TOKEN",
                "allowed_tools": ["Bash", "Read", "Edit"],
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.allowed_tools == ["Bash", "Read", "Edit"]


def test_provider_dict_form_default_allowed_tools_is_empty_list():
    # Arrange — back-compat: omit allowed_tools entirely.
    spec = {
        "claude": {
            "provider": {
                "base_url": "http://127.0.0.1:4000",
                "auth_token_env": "VLLM_TOKEN",
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert — the runner treats [] as "use the runner default".
    assert result.provider.allowed_tools == []


def test_provider_dict_form_non_list_allowed_tools_coerces_to_empty():
    # Arrange — defensive: a string-not-list (yaml quirk) coerces to [].
    # The validator separately surfaces this as a loud config error;
    # the parser keeps the agent from crashing while validation runs.
    spec = {
        "claude": {
            "provider": {
                "base_url": "http://127.0.0.1:4000",
                "auth_token_env": "VLLM_TOKEN",
                "allowed_tools": "Bash,Read",
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.provider.allowed_tools == []


def test_provider_dict_form_non_string_entries_filtered():
    # Arrange — defensive: drop non-string entries so a partial-bad
    # yaml doesn't propagate ints/None into ClaudeAgentOptions.tools.
    spec = {
        "claude": {
            "provider": {
                "base_url": "http://127.0.0.1:4000",
                "auth_token_env": "VLLM_TOKEN",
                "allowed_tools": ["Bash", 42, None, "Read", ""],
            }
        }
    }
    # Act
    result = parse_claude(spec)
    # Assert — only the valid non-empty strings survive.
    assert result.provider.allowed_tools == ["Bash", "Read"]
