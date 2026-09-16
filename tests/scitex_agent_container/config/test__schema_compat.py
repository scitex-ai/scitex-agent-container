"""Canonical authoring validation at the v3 parser boundary."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._schema_compat import (
    canonical_surface_errors,
    normalize_document,
    select_harness_document,
)
from scitex_agent_container.config._validation import validate_raw


def _hermes_entry() -> dict:
    return {
        "session": {"mode": "continue", "max_age_minutes": None},
    }


def _claude_code_entry(*, account: str | None = None) -> dict:
    entry = {
        "session": {"mode": "continue", "max_age_minutes": None},
        "approval_policy": "never",
        "watchdog": {
            "enabled": False,
            "interval": 1.5,
            "responses": {"y_n": "1", "y_y_n": "2", "waiting": "wait"},
        },
    }
    if account is not None:
        entry["account"] = account
    return entry


def _comms(channels: list[str] | None = None) -> dict:
    return {
        "channels": (
            ["server:sac", "server:scitex-cards"] if channels is None else channels
        )
    }


def test_claude_code_account_is_accepted_and_folded_to_the_runtime_boundary():
    # Arrange -- the Hub-style canonical spec has no legacy spec.claude block.
    raw = {
        "spec": {
            "harness": "claude-code",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {
                "claude-code": _claude_code_entry(account="scitex-01-scitex-ai")
            },
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    normalized = normalize_document(raw)
    # Assert
    assert (errors, normalized["spec"]["claude"]["account"]) == (
        [],
        "scitex-01-scitex-ai",
    )


@pytest.mark.parametrize("account", [None, "", "   ", 1, [], {}])
def test_claude_code_account_must_be_a_non_empty_string(account):
    # Arrange
    entry = _claude_code_entry()
    entry["account"] = account
    raw = {
        "spec": {
            "harness": "claude-code",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"claude-code": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.claude-code.account must be a non-empty string"
    ]


def test_claude_code_account_rejects_ambiguous_surrounding_whitespace():
    # Arrange
    entry = _claude_code_entry(account=" scitex-01-scitex-ai ")
    raw = {
        "spec": {
            "harness": "claude-code",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"claude-code": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.claude-code.account must not have leading or "
        "trailing whitespace"
    ]


def test_account_is_rejected_on_a_harness_that_does_not_own_claude_oauth():
    # Arrange
    entry = _hermes_entry()
    entry["account"] = "scitex-01-scitex-ai"
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == ["spec.available_harnesses.hermes has unknown fields: ['account']"]


def test_canonical_hermes_harness_entry_is_accepted():
    # Arrange
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": _hermes_entry()},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == []


def test_canonical_hermes_compression_is_accepted():
    # Arrange
    entry = _hermes_entry()
    entry["compression"] = {
        "threshold": 0.40,
        "target_ratio": 0.15,
        "tail_mode": "legacy",
        "in_place": False,
    }
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == []


def test_canonical_hermes_background_review_is_accepted():
    # Arrange
    entry = _hermes_entry()
    entry["background_review"] = True
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == []


def test_canonical_hermes_run_budget_is_accepted():
    # Arrange
    entry = _hermes_entry()
    entry["run_budget_seconds"] = 90
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == []


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5, "90"])
def test_hermes_run_budget_requires_a_positive_integer(value):
    # Arrange
    entry = _hermes_entry()
    entry["run_budget_seconds"] = value
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.hermes.run_budget_seconds must be a positive integer"
    ]


@pytest.mark.parametrize("value", [None, 0, 1, "false", {}])
def test_hermes_background_review_requires_a_boolean(value):
    # Arrange
    entry = _hermes_entry()
    entry["background_review"] = value
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.hermes.background_review must be a boolean"
    ]


def test_background_review_is_rejected_on_non_hermes_harness():
    # Arrange
    raw = {
        "spec": {
            "harness": "codex",
            "runtime": "tui",
            "comms": _comms([]),
            "available_harnesses": {
                "codex": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "approval_policy": "never",
                    "sandbox_mode": "danger-full-access",
                    "background_review": False,
                }
            },
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.codex.background_review is only valid for the "
        "Hermes harness"
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("threshold", 0, "threshold must be a number between 0 and 1"),
        ("threshold", True, "threshold must be a number between 0 and 1"),
        ("target_ratio", 0, "target_ratio must be a number between 0 and 1"),
        ("tail_mode", "compact", "tail_mode must be lean or legacy"),
        ("in_place", "yes", "in_place must be a boolean"),
    ],
)
def test_hermes_compression_rejects_invalid_values(field, value, message):
    # Arrange
    entry = _hermes_entry()
    entry["compression"] = {field: value}
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert any(message in error for error in errors)


def test_hermes_compression_target_must_be_below_trigger():
    # Arrange
    entry = _hermes_entry()
    entry["compression"] = {"threshold": 0.40, "target_ratio": 0.40}
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert any("target_ratio must be less than threshold" in error for error in errors)


def test_compression_is_rejected_on_non_hermes_harness():
    # Arrange
    raw = {
        "spec": {
            "harness": "codex",
            "runtime": "tui",
            "comms": _comms([]),
            "available_harnesses": {
                "codex": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "approval_policy": "never",
                    "sandbox_mode": "danger-full-access",
                    "compression": {"threshold": 0.80},
                }
            },
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.codex.compression is only valid for the "
        "Hermes harness"
    ]


def test_canonical_harness_unknown_fields_reach_validation():
    # Arrange
    entry = _hermes_entry()
    entry["gateway"] = {"enabled": True}
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = validate_raw(raw, "/tmp/hermes/spec.yaml")
    # Assert
    assert any("unknown fields: ['gateway']" in error for error in errors)


def test_hermes_headless_is_rejected_during_v3_validation():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "harness": "hermes",
            "runtime": "headless",
            "comms": _comms(),
            "available_harnesses": {"hermes": _hermes_entry()},
        },
    }
    # Act
    errors = validate_raw(raw, "/tmp/hermes/spec.yaml")
    # Assert
    assert any(
        "Unsupported runtime" in error and "spec.harness='hermes'" in error
        for error in errors
    )


def test_channels_are_declared_once_outside_the_harness():
    # Arrange
    entry = _hermes_entry()
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(["cct", "cards", "sac"]),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = validate_raw(raw, "/tmp/hermes/spec.yaml")
    # Assert
    assert not any("channels" in error for error in errors)


def test_harness_specific_channels_are_rejected_with_migration_hint():
    # Arrange
    entry = _hermes_entry()
    entry["channels"] = ["server:sac"]
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == [
        "spec.available_harnesses.hermes.channels is harness-specific placement; "
        "move it to spec.comms.channels so the same declaration drives Claude Code, "
        "Hermes, and Codex"
    ]


@pytest.mark.parametrize(
    ("harness", "entry"),
    [
        (
            "claude-code",
            {
                "session": {"mode": "continue", "max_age_minutes": None},
                "approval_policy": "never",
                "watchdog": {
                    "enabled": False,
                    "interval": 1.5,
                    "responses": {"y_n": "1", "y_y_n": "2", "waiting": "wait"},
                },
            },
        ),
        ("hermes", _hermes_entry()),
        (
            "codex",
            {
                "session": {"mode": "continue", "max_age_minutes": None},
                "approval_policy": "never",
                "sandbox_mode": "danger-full-access",
            },
        ),
    ],
)
def test_neutral_channels_project_identically_for_each_harness(
    harness: str, entry: dict
) -> None:
    # Arrange
    channels = ["server:sac", "server:scitex-cards"]
    raw = {
        "spec": {
            "harness": harness,
            "runtime": "tui",
            "comms": _comms(channels),
            "available_harnesses": {harness: entry},
        }
    }
    # Act
    normalized = normalize_document(raw)
    # Assert
    assert normalized["spec"]["claude"]["channels"] == channels


def test_hermes_continue_age_is_rejected_until_the_runtime_enforces_it():
    # Arrange
    entry = _hermes_entry()
    entry["session"]["max_age_minutes"] = 60
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "comms": _comms(),
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = validate_raw(raw, "/tmp/hermes/spec.yaml")
    # Assert
    assert any(
        "available_harnesses.hermes.session.max_age_minutes must be null" in error
        for error in errors
    )


def test_launch_harness_selection_is_copying_and_exact():
    raw = {
        "spec": {
            "harness": "claude-code",
            "available_harnesses": {"claude-code": {}, "hermes": {}},
        }
    }

    selected = select_harness_document(raw, "hermes")

    assert (
        selected["spec"]["harness"],
        raw["spec"]["harness"],
    ) == ("hermes", "claude-code")


def test_unknown_launch_harness_lists_choices_and_never_falls_back():
    raw = {
        "spec": {
            "harness": "claude-code",
            "available_harnesses": {"claude-code": {}, "hermes": {}},
        }
    }

    with pytest.raises(ValueError) as caught:
        select_harness_document(raw, "codex")

    assert str(caught.value) == (
        "unknown harness 'codex'; available harnesses: 'claude-code', 'hermes'. "
        "An explicit --harness never falls back to spec.harness."
    )
