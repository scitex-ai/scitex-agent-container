"""Tests for ``runtimes._apptainer_auth_bind`` (credentials file-bind).

The bind half of ``_apptainer_auth`` was extracted here (512-line cap,
openai-compat-3). This file pins the extraction contract:

* the legacy import path still resolves (``_apptainer_auth`` re-exports
  every public bind name), so no caller changes on the split;
* ``credentials_file_bind`` gates OFF for an ``openai``-family launch
  (openai-compat-3) exactly as it always has for an Anthropic-compat
  ``spec.claude.provider`` override — an openai agent has no Anthropic
  credential to bind, even when the spec pins one.

The pre-existing bind behaviours (designated-file expiry gate, account
snapshot resolution, placeholder pre-create) keep their coverage in
``test__apptainer_build_argv.py`` / ``test__apptainer_auth_dir_bind.py``,
which exercise this module through the re-export — unchanged on purpose
to prove the split is behavior-preserving.

Real seams only (no mocks); AAA markers (TQ002); one observable fact
per test (TQ007).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes import _apptainer_auth, _apptainer_auth_bind
from scitex_agent_container.runtimes._apptainer_auth_bind import (
    credentials_file_bind,
)
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError


@pytest.fixture
def sandbox_env(tmp_path: Path, env_save_restore):
    """Sandbox ``$HOME`` + scrub the family override for determinism."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("SAC_PROVIDER")
    return env_save_restore


def _openai_config_with_pinned_file(creds: Path) -> AgentConfig:
    return AgentConfig(
        name="oai",
        runtime="apptainer",
        harness="openai",
        workdir="/tmp/oai-wd",
        claude=ClaudeSpec(credentials_file=str(creds)),
    )


# ---------------------------------------------------------------------------
# openai family → no credentials bind (openai-compat-3 gate)
# ---------------------------------------------------------------------------


def test_openai_family_spec_yields_no_credentials_bind(
    tmp_path: Path, sandbox_env
) -> None:
    # Arrange — a real pinned credentials file exists, but the launch
    # resolves to the openai family: nothing Anthropic may be bound.
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}")
    cfg = _openai_config_with_pinned_file(creds)
    # Act
    flags = credentials_file_bind(cfg)
    # Assert
    assert flags == []


def test_openai_family_gate_skips_the_expiry_check(
    tmp_path: Path, sandbox_env
) -> None:
    # Arrange — the pinned file has NO parsable OAuth expiry, which the
    # anthropic path refuses loudly (CredentialExpiredError). The openai
    # gate must return BEFORE that check: the credential is simply not
    # consulted for an openai-family launch.
    creds = tmp_path / ".credentials.json"
    creds.write_text("not-even-json")
    cfg = _openai_config_with_pinned_file(creds)
    # Act
    flags = credentials_file_bind(cfg)
    # Assert
    assert flags == []


def test_sac_provider_override_gates_off_the_bind(
    tmp_path: Path, sandbox_env
) -> None:
    # Arrange — an anthropic-family spec flipped to openai via the
    # SAC_PROVIDER ops-only override must also skip the bind.
    sandbox_env.set("SAC_PROVIDER", "openai")
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}")
    cfg = AgentConfig(
        name="flip",
        runtime="apptainer",
        workdir="/tmp/flip-wd",
        claude=ClaudeSpec(credentials_file=str(creds)),
    )
    # Act
    flags = credentials_file_bind(cfg)
    # Assert
    assert flags == []


@pytest.mark.parametrize("harness", ["hermes", "codex", "opencode"])
def test_non_claude_harness_never_binds_an_unused_claude_file(
    tmp_path: Path, sandbox_env, harness: str,
) -> None:
    # Arrange — a missing Claude file cannot serve these harnesses' auth.
    cfg = AgentConfig(name="other-harness", harness=harness, runtime="tui")
    cfg.claude.credentials_file = str(tmp_path / "missing-claude.json")
    # Act
    flags = credentials_file_bind(cfg)
    # Assert
    assert flags == []


@pytest.mark.parametrize("harness", ["anthropic", "claude-code"])
def test_claude_family_still_refuses_a_missing_pinned_file(
    tmp_path: Path, sandbox_env, harness: str,
) -> None:
    # Arrange — canonical and public alias still require their selected pin.
    cfg = AgentConfig(name="claude", harness=harness)
    cfg.claude.credentials_file = str(tmp_path / "missing-claude.json")
    # Act
    def bind():
        return credentials_file_bind(cfg)

    # Assert
    with pytest.raises(FileNotFoundError, match="credentials_file points at"):
        bind()


def test_unknown_harness_is_refused_before_credentials_are_resolved(
    tmp_path: Path, sandbox_env,
) -> None:
    # Arrange — an unknown selector cannot acquire a skip-auth privilege.
    cfg = AgentConfig(name="typo", harness="unknown-harness")
    cfg.claude.credentials_file = str(tmp_path / "missing-claude.json")
    # Act
    def bind():
        return credentials_file_bind(cfg)

    # Assert
    with pytest.raises(ProviderEnvError, match="unknown harness"):
        bind()


def test_provider_backed_claude_keeps_its_api_key_auth_route(
    tmp_path: Path, sandbox_env,
) -> None:
    # Arrange — a provider endpoint consumes its key, never this Claude pin.
    cfg = AgentConfig(name="provider", harness="anthropic")
    cfg.claude.credentials_file = str(tmp_path / "missing-claude.json")
    cfg.claude.provider = ProviderSpec(
        base_url="https://backend.invalid/v1", auth_token_env="TEST_PROVIDER_KEY"
    )
    # Act
    flags = credentials_file_bind(cfg)
    # Assert
    assert flags == []


# ---------------------------------------------------------------------------
# Extraction contract — the legacy import path resolves the SAME objects
# ---------------------------------------------------------------------------


def test_auth_module_reexports_credentials_file_bind(sandbox_env) -> None:
    # Arrange — callers import the bind from _apptainer_auth (pre-split path).
    # Act
    same = _apptainer_auth.credentials_file_bind is credentials_file_bind
    # Assert
    assert same is True


def test_auth_module_reexports_credential_expired_error(sandbox_env) -> None:
    # Arrange — the error type must stay catchable via its legacy path.
    # Act
    same = (
        _apptainer_auth.CredentialExpiredError
        is _apptainer_auth_bind.CredentialExpiredError
    )
    # Assert
    assert same is True


def test_auth_module_reexports_ensure_credentials_bind_target(sandbox_env) -> None:
    # Arrange — build_run_argv imports this from _apptainer_auth.
    # Act
    same = (
        _apptainer_auth.ensure_credentials_bind_target
        is _apptainer_auth_bind.ensure_credentials_bind_target
    )
    # Assert
    assert same is True
