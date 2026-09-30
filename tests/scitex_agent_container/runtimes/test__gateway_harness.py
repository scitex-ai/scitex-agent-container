"""Conformance tests for ``runtimes/_gateway_harness.py``.

The contract pins the per-agent spec rule: two agents with different
spec blocks compile different options; no host/global state is read.
Live-server paths are NOT exercised here (no gateway in CI).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._gateway_harness import (
    GATEWAY_HARNESSES,
    HERMES_GATEWAY,
    GatewayHarness,
    GatewayHarnessError,
    get_gateway_harness,
    heartbeat_observation_payload,
)


def _hermes_config(**overrides):
    base = {"name": "worker", "harness": "hermes", "runtime": "tui"}
    base.update(overrides)
    return AgentConfig(**base)


def test_hermes_adapter_satisfies_the_protocol():
    # Arrange
    adapter = HERMES_GATEWAY
    # Act
    conforms = isinstance(adapter, GatewayHarness)
    # Assert
    assert conforms


def test_hermes_adapter_registry_identity():
    # Arrange
    adapter = HERMES_GATEWAY
    # Act
    identity = (adapter.name, adapter.spec_key)
    # Assert
    assert identity == ("hermes-tui", "hermes")


def test_registry_resolves_hermes():
    # Arrange
    name = "hermes"
    # Act
    resolved = get_gateway_harness(name)
    # Assert
    assert resolved is HERMES_GATEWAY


def test_registry_holds_both_gateway_families():
    # Arrange
    registry = GATEWAY_HARNESSES
    # Act
    families = set(registry)
    # Assert
    assert families == {"hermes", "opencode"}


def test_registry_refuses_unknown_harness():
    # Arrange
    name = "tmux-typing"

    def action():
        return get_gateway_harness(name)

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="Unknown gateway harness"):
        run()


def test_heartbeat_payload_reports_busy_state():
    # Arrange
    states = {"sac:worker": "busy"}
    # Act
    payload = heartbeat_observation_payload(states, "sac:worker")
    # Assert
    assert payload["state"] == "busy"


def test_heartbeat_payload_tags_the_writer():
    # Arrange
    states = {"sac:worker": "busy"}
    # Act
    payload = heartbeat_observation_payload(states, "sac:worker")
    # Assert
    assert payload["writer"] == "gateway-observer"


def test_heartbeat_payload_merges_identity_fields():
    # Arrange
    states = {"sac:worker": "busy"}
    identity = {"agent_id": "worker", "harness": "hermes"}
    # Act
    payload = heartbeat_observation_payload(
        states, "sac:worker", identity_fields=identity
    )
    # Assert
    assert payload["agent_id"] == "worker"


def test_heartbeat_payload_marks_missing_sessions_unknown():
    # Arrange
    states = {"sac:worker": "busy"}
    # Act
    payload = heartbeat_observation_payload(states, "sac:other")
    # Assert
    assert payload["state"] == "unknown"


def test_per_agent_options_default_background_review_off():
    # Arrange
    config = _hermes_config()
    # Act
    options = HERMES_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["background_review"] is False


def test_per_agent_options_read_the_spec_block():
    # Arrange
    config = _hermes_config(hermes_background_review=True)
    # Act
    options = HERMES_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["background_review"] is True


def test_per_agent_options_carry_the_resolved_engine():
    # Arrange
    config = _hermes_config(engine_key="e", model="m")
    # Act
    options = HERMES_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["engine"] == {"key": "e", "model": "m"}


def test_per_agent_options_cover_the_contract_keys():
    # Arrange
    config = _hermes_config()
    # Act
    options = HERMES_GATEWAY.parse_agent_options(config)
    # Assert
    assert set(options) == {
        "session",
        "background_review",
        "run_budget_seconds",
        "compression",
        "engine",
    }


def test_owner_argv_refuses_an_unresolved_engine():
    # Arrange
    config = _hermes_config()

    def action():
        return HERMES_GATEWAY.owner_argv(config, state_dir="/tmp/state")

    # Act
    run = action
    # Assert
    with pytest.raises(ValueError, match="resolved engine"):
        run()


def test_hermes_has_no_run_level_abort_and_says_so():
    # Arrange
    config = _hermes_config()

    def action():
        return HERMES_GATEWAY.abort_turn("/tmp/state", config.name)

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="sac agents stop"):
        run()
