"""Immutable provider proof across the listen broker/host child boundary."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from scitex_agent_container._listen._provider_proof import (
    BROKERED_LIFECYCLE_ENV,
    EXPECTED_PROVIDER_PROOF_ENV,
    ProviderProofError,
    brokered_provider_proof_env,
    load_and_verify_brokered_provider_proof,
    provider_proof,
    provider_source_evidence,
    verify_brokered_provider_proof,
)
from scitex_agent_container.config import load_config


@contextmanager
def _swap(module, name: str, value):
    saved = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, saved)


def _install_provider_spec(tmp_path: Path, *, endpoint: str) -> Path:
    source = (
        Path(__file__).resolve().parents[3]
        / "examples/providers/opencode-go-hermes.yaml"
    )
    target = tmp_path / "proof-agent" / "spec.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        source.read_text(encoding="utf-8").replace(
            "https://opencode.ai/zen/go/v1", endpoint
        ),
        encoding="utf-8",
    )
    return target


def test_spec_swap_after_listener_preflight_is_rejected(tmp_path: Path) -> None:
    # Arrange — compute the listener's proof, then replace the valid spec before
    # the host child performs its own load.
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    listener_config = load_config(spec)
    expected = provider_proof(listener_config, spec)
    _install_provider_spec(tmp_path, endpoint="https://attacker.invalid/v1")
    child_config = load_config(spec)
    child_env = {
        BROKERED_LIFECYCLE_ENV: "1",
        EXPECTED_PROVIDER_PROOF_ENV: expected,
    }

    # Act
    with pytest.raises(ProviderProofError) as caught:
        verify_brokered_provider_proof(child_config, spec, environ=child_env)

    # Assert
    assert caught.value.category == "provider_proof_mismatch"


def test_brokered_child_without_expected_proof_fails_loud(tmp_path: Path) -> None:
    # Arrange
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    config = load_config(spec)

    # Act
    with pytest.raises(ProviderProofError) as caught:
        verify_brokered_provider_proof(
            config, spec, environ={BROKERED_LIFECYCLE_ENV: "1"}
        )

    # Assert
    assert caught.value.category == "provider_proof_missing"


def test_brokered_child_load_failure_is_loud_and_redacted(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    marker = "RAW-SPEC-CONTENT-MUST-NOT-APPEAR"
    env_save_restore.set(BROKERED_LIFECYCLE_ENV, "1")

    def failed_loader(_path):
        raise ValueError(marker)

    # Act
    with pytest.raises(ProviderProofError) as caught:
        load_and_verify_brokered_provider_proof(
            tmp_path / "missing.yaml", failed_loader
        )

    # Assert
    assert (
        caught.value.category,
        marker in str(caught.value),
    ) == ("provider_proof_child_load_failed", False)


def test_direct_host_start_without_broker_marker_remains_compatible(
    tmp_path: Path,
) -> None:
    # Arrange
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    config = load_config(spec)

    # Act
    result = verify_brokered_provider_proof(config, spec, environ={})

    # Assert
    assert result is None


def test_provider_proof_is_value_free_and_fixed_length(tmp_path: Path) -> None:
    # Arrange
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    config = load_config(spec)
    canary_value = "provider-secret-must-not-appear"
    os.environ["OPENCODE_GO_API_KEY"] = canary_value
    try:
        # Act
        proof = provider_proof(config, spec)
    finally:
        os.environ.pop("OPENCODE_GO_API_KEY", None)

    # Assert
    assert (
        proof.startswith("v1:sha256:")
        and len(proof) == 74
        and canary_value not in proof
    )


@pytest.mark.parametrize(
    "field",
    ["engine_key", "model", "base_url", "auth_env", "headers", "protocol"],
)
def test_every_provider_relevant_effective_field_changes_the_proof(
    tmp_path: Path, field: str
) -> None:
    # Arrange
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    config = load_config(spec)
    before = provider_proof(config, spec)
    provider = config.claude.provider
    assert provider is not None
    if field == "engine_key":
        config.engine_key = "different-engine"
    elif field == "model":
        config.claude.model = "different-model"
    elif field == "base_url":
        provider.base_url = "https://different.invalid/v1"
    elif field == "auth_env":
        provider.auth_token_env = "DIFFERENT_PROVIDER_KEY"
    elif field == "headers":
        provider.extra_headers = {"X-Proof-Canary": "different"}
    else:
        provider.base_url = "https://different.invalid/v1/responses"

    # Act
    after = provider_proof(config, spec)

    # Assert
    assert after != before


def test_listener_refuses_source_replacement_during_its_own_preflight(
    tmp_path: Path,
) -> None:
    # Arrange — even an atomic replacement with byte-identical content changes
    # source identity and must not be paired with the config loaded earlier.
    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    source_before = provider_source_evidence(spec)
    config = load_config(spec)
    replacement = spec.with_suffix(".replacement")
    replacement.write_bytes(spec.read_bytes())
    os.replace(replacement, spec)

    # Act
    with pytest.raises(ProviderProofError) as caught:
        brokered_provider_proof_env(
            config, spec, expected_source=source_before
        )

    # Assert
    assert caught.value.category == "provider_proof_source_changed"


def test_agent_start_checks_broker_proof_immediately_after_load(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._start import agent_start

    spec = _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    env_save_restore.set(BROKERED_LIFECYCLE_ENV, "1")
    env_save_restore.set(EXPECTED_PROVIDER_PROOF_ENV, "v1:sha256:" + "0" * 64)

    # Act
    with pytest.raises(ProviderProofError) as caught:
        agent_start(str(spec))

    # Assert
    assert caught.value.category == "provider_proof_mismatch"


def test_plain_restart_checks_broker_proof_before_any_restart_action(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle import _restart as restart_mod

    _install_provider_spec(tmp_path, endpoint="https://opencode.ai/zen/go/v1")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path))
    env_save_restore.set(BROKERED_LIFECYCLE_ENV, "1")
    env_save_restore.set(EXPECTED_PROVIDER_PROOF_ENV, "v1:sha256:" + "0" * 64)
    actions: list[str] = []

    def unexpected_restart(*_args, **_kwargs):
        actions.append("restart")
        return {"name": "proof-agent", "restarted": True}, True

    # Act
    with (
        _swap(restart_mod, "must_broker_to_host", lambda: False),
        _swap(restart_mod, "_restart_locally", unexpected_restart),
    ):
        out, ok = restart_mod._restart_one("proof-agent", as_json=True, fresh=False)

    # Assert
    assert (ok, out.get("error"), actions) == (
        False,
        "provider_proof_mismatch",
        [],
    )
