"""Canonical authoring validation at the v3 parser boundary."""

from __future__ import annotations

from scitex_agent_container.config._schema_compat import canonical_surface_errors
from scitex_agent_container.config._validation import validate_raw


def _hermes_entry() -> dict:
    return {
        "session": {"mode": "continue", "max_age_minutes": None},
        "channels": [],
    }


def test_canonical_hermes_harness_entry_is_accepted():
    # Arrange
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "available_harnesses": {"hermes": _hermes_entry()},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert errors == []


def test_canonical_harness_unknown_fields_reach_validation():
    # Arrange
    entry = _hermes_entry()
    entry["gateway"] = {"enabled": True}
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
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


def test_hermes_channels_are_rejected_until_the_tui_runtime_wires_them():
    # Arrange
    entry = _hermes_entry()
    entry["channels"] = ["telegram"]
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
            "available_harnesses": {"hermes": entry},
        }
    }
    # Act
    errors = validate_raw(raw, "/tmp/hermes/spec.yaml")
    # Assert
    assert any(
        "available_harnesses.hermes.channels must be empty" in error
        for error in errors
    )


def test_hermes_continue_age_is_rejected_until_the_runtime_enforces_it():
    # Arrange
    entry = _hermes_entry()
    entry["session"]["max_age_minutes"] = 60
    raw = {
        "spec": {
            "harness": "hermes",
            "runtime": "tui",
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
