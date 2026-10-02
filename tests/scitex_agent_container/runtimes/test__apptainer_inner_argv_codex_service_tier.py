"""Native profile contracts from the actual proposed Scholar manager spec."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import tomllib
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_types import EngineSpec, apply_engine
from scitex_agent_container.runtimes import _apptainer_codex_env as homes
from scitex_agent_container.runtimes._apptainer_inner_argv_codex import (
    codex_config_overrides,
)

FIXTURE = Path(__file__).parent / "_fixtures/scitex-scholar/spec.yaml"
ENGINE = "codex-gpt-6.1-sol-ultra-fast"


def overrides(config):
    flags = codex_config_overrides(config)
    return tomllib.loads("\n".join(flags[1::2]))


def load_changed(tmp_path, change):
    raw = yaml.safe_load(FIXTURE.read_text())
    change(raw["spec"])
    path = tmp_path / "scitex-scholar/spec.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("model", "gpt-6.1-sol"),
        ("model_provider", "openai"),
        ("model_reasoning_effort", "ultra"),
        ("service_tier", "fast"),
    ],
)
def test_actual_manager_profile_compiles_ultra_and_fast_independently(key, expected):
    # Arrange
    config = load_config(FIXTURE)
    # Act
    seen = overrides(config)
    # Assert
    assert seen[key] == expected


@pytest.mark.parametrize(
    "attribute,expected",
    [
        ("harness", "codex"),
        ("subscription_account", "openai:fleet-lead-native"),
        ("name", "scitex-scholar"),
    ],
)
def test_profile_retains_declared_identity(attribute, expected):
    # Arrange
    path = FIXTURE
    # Act
    config = load_config(path)
    # Assert
    assert getattr(config, attribute) == expected


def test_profile_retains_declared_workdir():
    # Arrange
    raw = yaml.safe_load(FIXTURE.read_text())
    # Act
    config = load_config(FIXTURE)
    # Assert
    assert config.workdir == raw["spec"]["workdir"]


def test_profile_retains_cards_identity():
    # Arrange
    path = FIXTURE
    # Act
    config = load_config(path)
    # Assert
    assert config.env["SCITEX_CARDS_AGENT_ID"] == "scitex-scholar"


def test_private_runtime_home_is_derived_from_state(tmp_path):
    # Arrange
    state = tmp_path / "runtime/scitex-scholar"
    # Act
    home = homes.resolve_codex_home(state)
    # Assert
    assert home == state / "codex-home"


def test_private_container_home_retains_agent_identity():
    # Arrange
    name = "scitex-scholar"
    # Act
    home = homes.container_codex_home(name)
    # Assert
    assert home == "/tmp/sac-scitex-scholar-codex-home"


def test_two_managers_have_distinct_private_homes():
    # Arrange
    names = ("scitex-app", "scitex-scholar")
    # Act
    homes_by_name = [homes.container_codex_home(name) for name in names]
    # Assert
    assert len(set(homes_by_name)) == len(names)


def test_load_and_compile_do_not_mutate_the_reviewed_spec():
    # Arrange
    original = FIXTURE.read_bytes()
    # Act
    overrides(load_config(FIXTURE))
    # Assert
    assert FIXTURE.read_bytes() == original


@pytest.mark.parametrize("tier", ["priority", "default", "fastest", 1, False, {}, []])
def test_unknown_tiers_are_rejected_at_real_load_boundary(tmp_path, tier):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="service_tier"):
        load_changed(
            tmp_path,
            lambda spec: spec["available_engines"][ENGINE].update(service_tier=tier),
        )


def test_fast_cannot_be_selected_on_another_harness(tmp_path):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="service_tier"):
        load_changed(tmp_path, lambda spec: spec.update(harness="hermes"))


def test_fast_cannot_be_selected_for_inline_provider(tmp_path):
    # Arrange
    # Act
    def change(spec):
        engine = spec["available_engines"][ENGINE]
        del engine["subscription"]
        engine["provider"] = {
            "base_url": "https://example.invalid",
            "auth_token_env": "TEST_KEY",
        }

    # Assert
    with pytest.raises(ValueError, match="service_tier"):
        load_changed(tmp_path, change)


def test_omitted_tier_preserves_existing_generation(tmp_path):
    # Arrange
    # Act
    config = load_changed(
        tmp_path, lambda spec: spec["available_engines"][ENGINE].pop("service_tier")
    )
    # Assert
    assert "service_tier" not in overrides(config)


def test_engine_switch_clears_previous_fast_tier():
    # Arrange
    selected = load_config(FIXTURE)
    # Act
    apply_engine(
        selected,
        EngineSpec(
            key="native-standard",
            model="gpt-6.1-sol",
            subscription_provider="openai",
            subscription_account="openai:fleet-lead-native",
        ),
    )
    # Assert
    assert "service_tier" not in overrides(selected)


@pytest.fixture
def native_transport():
    """The real codec shared by launch and the SDK runner; no auth staging."""
    config = load_config(FIXTURE)
    config.runtime = "headless"
    flags = homes._codex_sdk_routing_flags(config)
    return dict(
        value.partition("=")[::2]
        for index, value in enumerate(flags)
        if index and flags[index - 1] == "--env"
    )


@pytest.mark.parametrize(
    "key,expected", [("service_tier", "fast"), ("model_reasoning_effort", "ultra")]
)
def test_native_sdk_transport_retains_generation_settings(
    native_transport, key, expected
):
    # Arrange
    encoded = native_transport["SAC_CODEX_CONFIG_OVERRIDES_B64"]
    # Act
    values = tomllib.loads("\n".join(json.loads(base64.b64decode(encoded))))
    # Assert
    assert values[key] == expected


def test_native_sdk_transport_preserves_declared_provider(native_transport):
    # Arrange
    env = native_transport
    # Act
    provider = env["SAC_CODEX_MODEL_PROVIDER"]
    # Assert
    assert provider == "openai"
