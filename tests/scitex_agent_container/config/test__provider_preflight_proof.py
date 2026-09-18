"""Immutable broker-to-child provider/spec proof."""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec


def _config(*, endpoint: str = "https://opencode.ai/zen/go/v1") -> AgentConfig:
    config = AgentConfig(name="proof-agent", harness="hermes")
    config.engine_key = "opencode-go-deepseek-v4.1-flash"
    config.claude.provider = ProviderSpec(
        base_url=endpoint,
        auth_token_env="OPENCODE_GO_API_KEY",
    )
    return config


def _verification_category(config: AgentConfig, proof: str) -> str:
    from scitex_agent_container.config._provider_preflight_proof import (
        PROVIDER_PREFLIGHT_PROOF_ENV,
        ProviderPreflightProofError,
        consume_provider_preflight_proof,
    )

    environ = {PROVIDER_PREFLIGHT_PROOF_ENV: proof}
    try:
        consume_provider_preflight_proof(config, environ=environ)
    except ProviderPreflightProofError as exc:
        return exc.category
    return "accepted"


def test_matching_provider_spec_proof_is_consumed() -> None:
    # Arrange
    from scitex_agent_container.config._provider_preflight_proof import (
        PROVIDER_PREFLIGHT_PROOF_ENV,
        consume_provider_preflight_proof,
        provider_preflight_proof,
    )

    config = _config()
    environ = {PROVIDER_PREFLIGHT_PROOF_ENV: provider_preflight_proof(config)}
    # Act
    consume_provider_preflight_proof(config, environ=environ)
    # Assert
    assert PROVIDER_PREFLIGHT_PROOF_ENV not in environ


def test_provider_endpoint_swap_after_preflight_is_refused() -> None:
    # Arrange
    from scitex_agent_container.config._provider_preflight_proof import (
        provider_preflight_proof,
    )

    proof = provider_preflight_proof(_config())
    swapped = _config(endpoint="https://attacker.invalid/v1")
    # Act
    category = _verification_category(swapped, proof)
    # Assert
    assert category == "provider_config_mismatch"


def test_non_provider_spec_swap_after_preflight_is_refused() -> None:
    # Arrange
    from scitex_agent_container.config._provider_preflight_proof import (
        provider_preflight_proof,
    )

    original = _config()
    swapped = _config()
    swapped.workdir = "/tmp/attacker-workdir"
    # Act
    differs = provider_preflight_proof(original) != provider_preflight_proof(swapped)
    # Assert
    assert differs is True


def test_create_after_absent_preflight_is_refused() -> None:
    # Arrange
    from scitex_agent_container.config._provider_preflight_proof import (
        absent_provider_preflight_proof,
    )

    proof = absent_provider_preflight_proof("proof-agent")
    # Act
    category = _verification_category(_config(), proof)
    # Assert
    assert category == "provider_config_mismatch"


def test_local_start_without_broker_proof_remains_allowed() -> None:
    # Arrange
    from scitex_agent_container.config._provider_preflight_proof import (
        consume_provider_preflight_proof,
    )

    environ: dict[str, str] = {}
    # Act
    consume_provider_preflight_proof(_config(), environ=environ)
    # Assert
    assert environ == {}


def test_agent_start_refuses_mismatch_before_runtime_construction(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle.lifecycle import agent_start
    from scitex_agent_container.config._provider_preflight_proof import (
        PROVIDER_PREFLIGHT_PROOF_ENV,
        ProviderPreflightProofError,
        provider_preflight_proof,
    )

    spec = tmp_path / "spec.yaml"
    spec.write_text("placeholder", encoding="utf-8")
    env_save_restore.set(PROVIDER_PREFLIGHT_PROOF_ENV, provider_preflight_proof(_config()))
    runtime_calls: list[str] = []

    def runtime_factory(_config):
        runtime_calls.append("constructed")
        raise AssertionError("runtime must not be constructed")

    # Act
    try:
        agent_start(
            str(spec),
            config_override=_config(endpoint="https://attacker.invalid/v1"),
            runtime_factory=runtime_factory,
            dry_run=True,
        )
    except ProviderPreflightProofError as exc:
        category = exc.category
    else:
        category = "not_refused"
    # Assert
    assert (category, runtime_calls) == ("provider_config_mismatch", [])


def test_agent_restart_refuses_mismatch_before_successor_or_stop(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    import yaml

    from scitex_agent_container._lifecycle.lifecycle import agent_restart
    from scitex_agent_container._state.registry import Registry
    from scitex_agent_container.config import load_config
    from scitex_agent_container.config._provider_preflight_proof import (
        PROVIDER_PREFLIGHT_PROOF_ENV,
        ProviderPreflightProofError,
        provider_preflight_proof,
    )
    from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

    spec = tmp_path / "proof-agent" / "spec.yaml"
    spec.parent.mkdir()
    original_doc = explicit_doc({"workdir": "/tmp/original"})
    spec.write_text(yaml.safe_dump(original_doc, sort_keys=False), encoding="utf-8")
    proof = provider_preflight_proof(load_config(spec))
    swapped_doc = explicit_doc({"workdir": "/tmp/swapped"})
    spec.write_text(yaml.safe_dump(swapped_doc, sort_keys=False), encoding="utf-8")
    env_save_restore.set(PROVIDER_PREFLIGHT_PROOF_ENV, proof)
    successor_calls: list[str] = []

    def successor_check(_path: str) -> None:
        successor_calls.append("checked")

    # Act
    try:
        agent_restart(
            "proof-agent",
            registry=Registry(registry_dir=tmp_path / "registry"),
            config_resolver=lambda _name: str(spec),
            successor_auth_check=successor_check,
        )
    except ProviderPreflightProofError as exc:
        category = exc.category
    else:
        category = "not_refused"
    # Assert
    assert (category, successor_calls) == ("provider_config_mismatch", [])
