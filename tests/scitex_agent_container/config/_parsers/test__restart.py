"""Tests for ``parse_restart`` + ``restart.prune_on_stop`` validation.

The ``prune_on_stop`` opt-in gates the inode-hygiene prune (see
``_lifecycle._prune_runtime``): it must default False (so a spec that
merely omits it is never pruned) and be rejected as an error when a
non-boolean is supplied.

AAA markers + one-fact-per-test per the package TQ convention.
"""

from __future__ import annotations

from scitex_agent_container.config._parsers._restart import parse_restart
from scitex_agent_container.config._validation import validate_raw


def test_parse_restart_prune_on_stop_defaults_false():
    # Arrange
    spec = {"restart": {"policy": "never", "max_retries": 3}}
    # Act
    got = parse_restart(spec)
    # Assert
    assert got.prune_on_stop is False


def test_parse_restart_prune_on_stop_true_when_set():
    # Arrange
    spec = {"restart": {"policy": "never", "max_retries": 3, "prune_on_stop": True}}
    # Act
    got = parse_restart(spec)
    # Assert
    assert got.prune_on_stop is True


def _min_raw(restart: dict) -> dict:
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"restart": restart},
    }


def test_validation_rejects_non_bool_prune_on_stop():
    # Arrange
    raw = _min_raw({"policy": "never", "max_retries": 3, "prune_on_stop": "yes"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("prune_on_stop must be a boolean" in e for e in errors)


def test_validation_accepts_bool_prune_on_stop():
    # Arrange
    raw = _min_raw({"policy": "never", "max_retries": 3, "prune_on_stop": True})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not any("prune_on_stop" in e for e in errors)
