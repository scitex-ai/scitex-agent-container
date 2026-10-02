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


def test_actual_manager_profile_compiles_ultra_and_fast_independently(
    tmp_path, monkeypatch
):
    raw = yaml.safe_load(FIXTURE.read_text())
    config = load_config(FIXTURE)
    seen = overrides(config)
    assert seen["model"] == "gpt-6.1-sol"
    assert seen["model_provider"] == "openai"
    assert seen["model_reasoning_effort"] == "ultra"
    assert seen["service_tier"] == "fast"
    assert config.harness == "codex"
    assert config.subscription_account == "openai:fleet-lead-native"
    assert config.name == "scitex-scholar"
    assert config.workdir == raw["spec"]["workdir"]
    assert config.env["SCITEX_CARDS_AGENT_ID"] == "scitex-scholar"
    monkeypatch.delenv("CODEX_HOME", raising=False)
    state = tmp_path / "runtime/scitex-scholar"
    assert homes.resolve_codex_home(state) == state / "codex-home"
    assert (
        homes.container_codex_home(config.name) == "/tmp/sac-scitex-scholar-codex-home"
    )
    assert homes.container_codex_home("scitex-app") != homes.container_codex_home(
        config.name
    )
    assert raw == yaml.safe_load(FIXTURE.read_text())


@pytest.mark.parametrize("tier", ["priority", "default", "fastest", 1, False, {}, []])
def test_unknown_tiers_are_rejected_at_real_load_boundary(tmp_path, tier):
    with pytest.raises(ValueError, match="service_tier"):
        load_changed(
            tmp_path,
            lambda spec: spec["available_engines"][ENGINE].update(service_tier=tier),
        )


def test_fast_cannot_be_selected_on_another_harness(tmp_path):
    with pytest.raises(ValueError, match="service_tier"):
        load_changed(tmp_path, lambda spec: spec.update(harness="hermes"))


def test_fast_cannot_be_selected_for_inline_provider(tmp_path):
    def change(spec):
        engine = spec["available_engines"][ENGINE]
        del engine["subscription"]
        engine["provider"] = {
            "base_url": "https://example.invalid",
            "auth_token_env": "TEST_KEY",
        }

    with pytest.raises(ValueError, match="service_tier"):
        load_changed(tmp_path, change)


def test_omitted_tier_preserves_existing_generation_and_engine_switch_clears_it(
    tmp_path,
):
    config = load_changed(
        tmp_path, lambda spec: spec["available_engines"][ENGINE].pop("service_tier")
    )
    assert "service_tier" not in overrides(config)
    selected = load_config(FIXTURE)
    apply_engine(
        selected,
        EngineSpec(
            key="native-standard",
            model="gpt-6.1-sol",
            subscription_provider="openai",
            subscription_account="openai:fleet-lead-native",
        ),
    )
    assert "service_tier" not in overrides(selected)


def test_declared_native_sdk_fast_is_transported_without_staging_real_auth(
    tmp_path, monkeypatch
):
    config = load_config(FIXTURE)
    config.runtime = "headless"
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(homes, "sync_subscription_auth", lambda *args: None)
    flags = homes.codex_env_flags(config, tmp_path / "private-runtime")
    env = dict(
        value.partition("=")[::2]
        for index, value in enumerate(flags)
        if index and flags[index - 1] == "--env"
    )
    values = tomllib.loads(
        "\n".join(json.loads(base64.b64decode(env["SAC_CODEX_CONFIG_OVERRIDES_B64"])))
    )
    assert values["service_tier"] == "fast"
    assert values["model_reasoning_effort"] == "ultra"
    assert env["SAC_CODEX_MODEL_PROVIDER"] == "openai"
