"""Tests for ``spec.claude.provider`` validation (backend override).

The provider override lets an agent run its SDK session against an
Anthropic-SDK-compatible backend (DeepSeek, gateway, ...) on an API key
instead of Anthropic OAuth. Validation rules:

* When provider is present, both ``base_url`` and ``auth_token_env`` are
  required (non-empty strings); an incomplete override would silently
  fall back to Anthropic at runtime.
* When provider is present, the claude-* model regex is relaxed so the
  provider's own model id (e.g. ``deepseek-chat``) validates cleanly.
* When provider is ABSENT, model validation is unchanged — a non-claude
  model id is still rejected.
* ``provider`` and ``account`` are mutually exclusive.

Each test pins one observable fact (TQ007) with AAA markers (TQ002) and
a descriptive name (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.config._validation import validate_raw

# Red-start ruling 2026-07-21: every spec field explicit — base fixture
# merges the validator's own paste defaults beneath the curated fields.
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": explicit_spec(
        {
            "runtime": "apptainer",
            "host": "${HOSTNAME}",
            "workdir": "/home/agent/work",
            "apptainer": {"image": "/x.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        }
    ),
}

_PROVIDER = {
    "base_url": "https://api.deepseek.com/anthropic",
    "auth_token_env": "DEEPSEEK_API_KEY",
}


def _claude_spec(claude: dict) -> dict:
    # Merge onto the explicit claude defaults so the block keeps every
    # required key while the test's fields win.
    merged = {**_BASE["spec"]["claude"], **claude}
    return {**_BASE, "spec": {**_BASE["spec"], "claude": merged}}


# ---------------------------------------------------------------------------
# Model-regex relaxation
# ---------------------------------------------------------------------------


def test_deepseek_model_accepted_when_provider_present():
    # Arrange
    raw = _claude_spec({"model": "deepseek-chat", "provider": _PROVIDER})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.model" in e] == []


def test_deepseek_model_rejected_when_provider_absent():
    # Arrange
    raw = _claude_spec({"model": "deepseek-chat"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.model" in e]


# ---------------------------------------------------------------------------
# Required-field enforcement
# ---------------------------------------------------------------------------


def test_provider_missing_base_url_is_rejected():
    # Arrange
    raw = _claude_spec(
        {"model": "deepseek-chat", "provider": {"auth_token_env": "DEEPSEEK_API_KEY"}}
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.provider.base_url" in e]


def test_provider_missing_auth_token_env_is_rejected():
    # Arrange
    raw = _claude_spec(
        {
            "model": "deepseek-chat",
            "provider": {"base_url": "https://api.deepseek.com/anthropic"},
        }
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.provider.auth_token_env" in e]


def test_provider_empty_base_url_is_rejected():
    # Arrange
    raw = _claude_spec(
        {
            "model": "deepseek-chat",
            "provider": {"base_url": "", "auth_token_env": "DEEPSEEK_API_KEY"},
        }
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.provider.base_url" in e]


def test_provider_non_string_base_url_is_rejected():
    # Arrange
    raw = _claude_spec(
        {
            "model": "deepseek-chat",
            "provider": {"base_url": 123, "auth_token_env": "DEEPSEEK_API_KEY"},
        }
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.provider.base_url must be a string" in e]


# ---------------------------------------------------------------------------
# Mutual exclusion with spec.claude.account
# ---------------------------------------------------------------------------


def test_provider_and_account_together_are_rejected():
    # Arrange
    raw = _claude_spec(
        {"model": "deepseek-chat", "account": "work", "provider": _PROVIDER}
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "mutually exclusive" in e]


# ---------------------------------------------------------------------------
# No-provider path is unchanged
# ---------------------------------------------------------------------------


def test_complete_provider_block_validates_clean():
    # Arrange
    raw = _claude_spec({"model": "deepseek-chat", "provider": _PROVIDER})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert errors == []


def test_account_only_without_provider_validates_clean():
    # Arrange — account without provider stays the OAuth-pin happy path.
    raw = _claude_spec({"model": "opus", "account": "work"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert errors == []
