"""``spec.autonomous`` + ``kind: AgentProxy`` coupling validation.

Real ``validate_autonomous`` / ``validate_proxy_coupling`` on real
dicts, no mocks of the code under test.
"""

from __future__ import annotations

from scitex_agent_container.config._shape_validation import (
    validate_autonomous,
    validate_proxy_coupling,
)


# ---------------------------------------------------------------------------
# validate_autonomous
# ---------------------------------------------------------------------------


def test_autonomous_absent_produces_no_error():
    # Arrange — absence means "feature off", not a hidden default.
    spec: dict = {}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert errors == []


def test_autonomous_non_mapping_is_rejected():
    # Arrange
    spec = {"autonomous": "yes"}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("must be a mapping" in e for e in errors)


def test_autonomous_empty_drive_until_is_rejected():
    # Arrange
    spec = {"autonomous": {"drive_until": ""}}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("drive_until must be non-empty" in e for e in errors)


def test_autonomous_non_string_drive_until_is_rejected():
    # Arrange
    spec = {"autonomous": {"drive_until": 5}}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("drive_until must be a string" in e for e in errors)


def test_autonomous_non_int_max_turns_is_rejected():
    # Arrange
    spec = {"autonomous": {"drive_until": "done", "max_turns": "lots"}}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("max_turns must be an integer" in e for e in errors)


def test_autonomous_non_positive_idle_kick_is_rejected():
    # Arrange
    spec = {"autonomous": {"drive_until": "done", "idle_kick_after_s": 0}}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("idle_kick_after_s must be > 0" in e for e in errors)


def test_autonomous_non_bool_enabled_is_rejected():
    # Arrange
    spec = {"autonomous": {"drive_until": "done", "enabled": "true"}}
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert any("enabled must be a boolean" in e for e in errors)


def test_autonomous_valid_block_produces_no_error():
    # Arrange
    spec = {
        "autonomous": {
            "drive_until": "all green",
            "max_turns": 10,
            "idle_kick_after_s": 30,
            "kick_text": "continue",
            "enabled": True,
        }
    }
    # Act
    errors = validate_autonomous(spec)
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# validate_proxy_coupling
# ---------------------------------------------------------------------------


def test_agentproxy_without_proxy_block_is_rejected():
    # Arrange — no upstream to forward to.
    spec: dict = {}
    # Act
    errors = validate_proxy_coupling(spec, "AgentProxy")
    # Assert
    assert any("spec.proxy is required" in e for e in errors)


def test_agentproxy_proxy_without_upstream_is_rejected():
    # Arrange
    spec = {"proxy": {}}
    # Act
    errors = validate_proxy_coupling(spec, "AgentProxy")
    # Assert
    assert any("spec.proxy.upstream is REQUIRED" in e for e in errors)


def test_agentproxy_with_forbidden_claude_is_rejected():
    # Arrange — a proxy has no SDK to configure.
    spec = {"proxy": {"upstream": "http://127.0.0.1:9000"}, "claude": {"model": "haiku"}}
    # Act
    errors = validate_proxy_coupling(spec, "AgentProxy")
    # Assert
    assert any("not allowed when kind: AgentProxy" in e for e in errors)


def test_agentproxy_with_valid_proxy_produces_no_error():
    # Arrange
    spec = {"proxy": {"upstream": "http://127.0.0.1:9000"}}
    # Act
    errors = validate_proxy_coupling(spec, "AgentProxy")
    # Assert
    assert errors == []


def test_agent_kind_with_proxy_is_rejected():
    # Arrange — the SDK runner doesn't read spec.proxy.
    spec = {"proxy": {"upstream": "http://127.0.0.1:9000"}}
    # Act
    errors = validate_proxy_coupling(spec, "Agent")
    # Assert
    assert any("only meaningful when kind: AgentProxy" in e for e in errors)


def test_agent_kind_without_proxy_produces_no_error():
    # Arrange
    spec = {"claude": {"model": "haiku"}}
    # Act
    errors = validate_proxy_coupling(spec, "Agent")
    # Assert
    assert errors == []
