"""Adversarial tests for the external-gateway credential boundary."""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._types import ApptainerSpec, ClaudeSpec
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_external_gateway import (
    external_gateway_active,
    remove_vendor_credential_flags,
)
from scitex_agent_container.runtimes._apptainer_secret_env import (
    secret_env_file_path,
)


def _gateway_config(*, harness: str = "hermes") -> AgentConfig:
    config = AgentConfig(
        name="flash-agent",
        runtime="tui",
        harness=harness,
        model="neutral-model-spelling",
        workdir="/work",
    )
    config.claude = ClaudeSpec(
        model="neutral-model-spelling",
        provider=ProviderSpec(
            base_url="http://scitex-compute-04:18775/",
            auth_token_env="CUSTOM_LOCAL_GATEWAY_TOKEN",
        ),
    )
    return config


def test_gateway_recognition_is_independent_of_model_and_token_env() -> None:
    # Arrange
    config = _gateway_config()
    # Act
    active = external_gateway_active(config)
    # Assert
    assert active is True


def test_explicit_vendor_values_are_removed_in_both_argv_spellings() -> None:
    # Arrange
    config = _gateway_config()
    argv = [
        "--env",
        "DEEPSEEK_API_KEY=spec-vendor",
        "--env=DEEPSEEK_API_KEY=raw-vendor",
        "--env",
        "KEEP=value",
    ]
    # Act
    result = remove_vendor_credential_flags(argv, config)
    # Assert
    assert result == ["--env", "KEEP=value"]


def test_codex_final_argv_denies_host_spec_raw_and_env_file_bypasses(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("CUSTOM_LOCAL_GATEWAY_TOKEN", "local-gateway-token")
    env_save_restore.set("DEEPSEEK_API_KEY", "host-vendor")
    attacker_env = tmp_path / "attacker.env"
    attacker_env.write_text("DEEPSEEK_API_KEY=file-vendor\n", encoding="utf-8")
    config = _gateway_config(harness="codex")
    config.env = {"DEEPSEEK_API_KEY": "spec-vendor"}
    config.apptainer = ApptainerSpec(
        raw_args=[
            "--env",
            "DEEPSEEK_API_KEY=raw-vendor",
            "--env-file",
            str(attacker_env),
        ]
    )
    state_dir = tmp_path / "state"
    # Act
    argv = build_run_argv(
        config,
        state_dir=state_dir,
        sif_path=tmp_path / "sac-base.sif",
        tui=True,
    )
    rendered_secret_file = secret_env_file_path(state_dir).read_text(encoding="utf-8")
    # Assert
    assert (
        argv.count("DEEPSEEK_API_KEY="),
        "spec-vendor" in str(argv),
        "raw-vendor" in str(argv),
        "host-vendor" in str(argv),
        "file-vendor" in rendered_secret_file,
        argv.index("DEEPSEEK_API_KEY=") > argv.index(str(attacker_env)),
    ) == (1, False, False, False, False, True)
