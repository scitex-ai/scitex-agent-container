"""``config._grants`` — the spec.grants schema, its validation and the gate.

Pure functions over plain mappings and real ``Grants`` values; no mocks.
"""

from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.config._grants import (
    GRANT_KEYS,
    Grants,
    all_granted,
    granted,
    grants_value_errors,
    host_exec_state,
    network_refusal,
    parse_grants,
)


def test_absent_grants_key_parses_to_none() -> None:
    # Arrange
    spec = {"workdir": "/w"}
    # Act
    parsed = parse_grants(spec)
    # Assert
    assert parsed is None


def test_empty_grants_block_withholds_everything() -> None:
    # Arrange
    spec = {"grants": {}}
    # Act
    parsed = parse_grants(spec)
    # Assert
    assert parsed == Grants()


def test_list_grant_parts_parse_to_frozenset() -> None:
    # Arrange
    spec = {"grants": {"host_binds": ["cards", "host_tmp"]}}
    # Act
    parsed = parse_grants(spec)
    # Assert
    assert parsed.host_binds == frozenset({"cards", "host_tmp"})


def test_legacy_config_is_granted_everything() -> None:
    # Arrange
    config = SimpleNamespace(grants=None)
    # Act
    answers = [granted(config, "fleet_env"), granted(config, "secrets", "envrc")]
    # Assert
    assert answers == [True, True]


def test_explicit_block_withholds_unnamed_part() -> None:
    # Arrange
    config = SimpleNamespace(grants=Grants(host_binds=frozenset({"cards"})))
    # Act
    answer = granted(config, "host_binds", "host_tmp")
    # Assert
    assert answer is False


def test_explicit_block_grants_named_part() -> None:
    # Arrange
    config = SimpleNamespace(grants=Grants(host_binds=frozenset({"cards"})))
    # Act
    answer = granted(config, "host_binds", "cards")
    # Assert
    assert answer is True


def test_all_granted_block_round_trips_through_parse() -> None:
    # Arrange
    block = all_granted().to_block()
    # Act
    parsed = parse_grants({"grants": block})
    # Assert
    assert parsed == all_granted()


def test_to_block_lists_keys_in_canonical_order() -> None:
    # Arrange
    grants = Grants()
    # Act
    keys = list(grants.to_block())
    # Assert
    assert keys == list(GRANT_KEYS)


def test_unknown_grant_key_is_a_validation_error() -> None:
    # Arrange
    spec = {"grants": {"host_everything": True}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("unknown keys" in e for e in errors)


def test_non_bool_grant_is_a_validation_error() -> None:
    # Arrange
    spec = {"grants": {"fleet_env": "yes"}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert errors == ["spec.grants.fleet_env must be true or false"]


def test_unknown_bind_name_is_a_validation_error() -> None:
    # Arrange
    spec = {"grants": {"host_binds": ["whole_home"]}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("unknown parts" in e for e in errors)


def test_host_exec_without_sac_control_is_refused() -> None:
    # Arrange
    spec = {"grants": {"host_exec": True}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("requires spec.grants.sac_control" in e for e in errors)


def test_hand_declared_sac_channel_needs_sac_control() -> None:
    # Arrange
    spec = {"grants": {}, "comms": {"channels": ["server:sac"]}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("server:sac" in e for e in errors)


def test_host_network_contradicts_sealed_sandbox() -> None:
    # Arrange
    spec = {"grants": {"host_network": True}, "sandbox": {"egress": []}}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("contradicts spec.sandbox" in e for e in errors)


def test_shared_layers_need_baseline_grant() -> None:
    # Arrange
    spec = {"grants": {}, "to_home_layers": ["user-shared", "per-agent"]}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert any("'baseline'" in e for e in errors)


def test_complete_block_validates_without_errors() -> None:
    # Arrange
    spec = {"grants": all_granted().to_block()}
    # Act
    errors = grants_value_errors(spec)
    # Assert
    assert errors == []


def test_unsealed_block_without_network_is_refused() -> None:
    # Arrange
    config = SimpleNamespace(name="a", grants=Grants(), sandbox=None)
    # Act
    refusal = network_refusal(config)
    # Assert
    assert "host_network" in refusal


def test_sealed_block_without_network_launches() -> None:
    # Arrange
    config = SimpleNamespace(name="a", grants=Grants(), sandbox=object())
    # Act
    refusal = network_refusal(config)
    # Assert
    assert refusal is None


def test_legacy_eligible_group_holds_host_exec() -> None:
    # Arrange
    config = SimpleNamespace(
        grants=None, sandbox=None, labels={"groups": ["developer"]}
    )
    # Act
    held, _why = host_exec_state(config)
    # Assert
    assert held is True


def test_explicit_block_without_host_exec_withholds_it() -> None:
    # Arrange
    config = SimpleNamespace(
        grants=Grants(sac_control=True), sandbox=None, labels={"groups": ["developer"]}
    )
    # Act
    held, _why = host_exec_state(config)
    # Assert
    assert held is False
