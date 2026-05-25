"""Tests for ``runtimes._apptainer_provider`` (backend override env).

The provider helper renders the three ``--env`` flags that point an
agent's SDK session at an Anthropic-SDK-compatible backend on an API
key (DeepSeek, gateway, ...). It fails loud rather than fall back to
Anthropic when the override cannot be satisfied.

Real seams only (no mocks): ``$DEEPSEEK_API_KEY`` is set / cleared via
the ``env_save_restore`` fixture; configs are real ``AgentConfig``
dataclasses.

Each test pins one observable fact (TQ007) with AAA markers (TQ002)
and a descriptive name (TQ003).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes._apptainer_provider import (
    ProviderEnvError,
    provider_active,
    provider_env_flags,
)


def _provider_config(name: str = "ds", **claude_kw) -> AgentConfig:
    claude = ClaudeSpec(
        model="deepseek-chat",
        provider=ProviderSpec(
            base_url="https://api.deepseek.com/anthropic",
            auth_token_env="DEEPSEEK_API_KEY",
        ),
        **claude_kw,
    )
    return AgentConfig(
        name=name, runtime="apptainer", workdir="/tmp/ds-wd", claude=claude
    )


def _env_dict(flags: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(flags):
        if a == "--env" and i + 1 < len(flags) and "=" in flags[i + 1]:
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# provider_active predicate
# ---------------------------------------------------------------------------


def test_provider_active_true_for_config_with_base_url():
    # Arrange
    cfg = _provider_config()
    # Act
    active = provider_active(cfg)
    # Assert
    assert active is True


def test_provider_active_false_for_config_without_provider():
    # Arrange
    cfg = AgentConfig(
        name="plain", runtime="apptainer", workdir="/tmp/x", claude=ClaudeSpec()
    )
    # Act
    active = provider_active(cfg)
    # Assert
    assert active is False


# ---------------------------------------------------------------------------
# Env injection — happy path
# ---------------------------------------------------------------------------


def test_flags_inject_anthropic_base_url(env_save_restore):
    # Arrange
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"


def test_flags_bridge_host_key_to_sac_anthropic_api_key(env_save_restore):
    # Arrange
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-deepseek-secret"


def test_flags_set_per_agent_clean_config_dir(env_save_restore):
    # Arrange — the conflict-breaker dir is namespaced by agent name.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(name="bulk7")
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/sac-bulk7-provider-cfg"


# ---------------------------------------------------------------------------
# Inactive path
# ---------------------------------------------------------------------------


def test_flags_empty_when_no_provider():
    # Arrange
    cfg = AgentConfig(
        name="plain", runtime="apptainer", workdir="/tmp/x", claude=ClaudeSpec()
    )
    # Act
    flags = provider_env_flags(cfg)
    # Assert
    assert flags == []


# ---------------------------------------------------------------------------
# Fail-loud paths
# ---------------------------------------------------------------------------


def test_unset_auth_token_env_raises_provider_env_error(env_save_restore):
    # Arrange — the named host env var is absent; no silent Anthropic fallback.
    env_save_restore.delete("DEEPSEEK_API_KEY")
    cfg = _provider_config()
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        provider_env_flags(cfg)


def test_unset_auth_token_env_error_names_the_env_var(env_save_restore):
    # Arrange
    env_save_restore.delete("DEEPSEEK_API_KEY")
    cfg = _provider_config()
    message = ""
    # Act
    try:
        provider_env_flags(cfg)
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "DEEPSEEK_API_KEY" in message


def test_provider_with_account_raises_provider_env_error(env_save_restore):
    # Arrange — provider + account is the mutually-exclusive collision.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(account="work")
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        provider_env_flags(cfg)


# ---------------------------------------------------------------------------
# to_home/.env fallback — operator-friendly key location
# ---------------------------------------------------------------------------


def _spec_with_to_home_env(tmp_path, dotenv_body: str):
    """Write a minimal spec dir with a ``to_home/.env`` file under tmp."""
    spec_dir = tmp_path / "agent"
    (spec_dir / "to_home").mkdir(parents=True)
    (spec_dir / "to_home" / ".env").write_text(dotenv_body)
    spec_yaml = spec_dir / "spec.yaml"
    spec_yaml.write_text("# placeholder — config_path only, never loaded\n")
    return spec_yaml


def test_auth_token_resolved_from_to_home_env_when_host_env_unset(
    env_save_restore, tmp_path
):
    # Arrange — host env intentionally empty; the operator dropped the
    # key in to_home/.env, which is the same file that ships into
    # $HOME/.env inside the container.
    env_save_restore.delete("DEEPSEEK_API_KEY")
    env_save_restore.delete("SAC_TO_HOME_BASELINE")
    spec_yaml = _spec_with_to_home_env(tmp_path, "DEEPSEEK_API_KEY=sk-from-to-home\n")
    cfg = _provider_config()
    cfg.config_path = str(spec_yaml)
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert — key bridged from to_home/.env into SAC_ANTHROPIC_API_KEY.
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-from-to-home"


def test_host_env_wins_over_to_home_env_when_both_set(env_save_restore, tmp_path):
    # Arrange — both sources populated with DIFFERENT values; the host
    # process env is the higher-priority source per the resolution
    # order documented in the module.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-from-host")
    env_save_restore.delete("SAC_TO_HOME_BASELINE")
    spec_yaml = _spec_with_to_home_env(tmp_path, "DEEPSEEK_API_KEY=sk-from-to-home\n")
    cfg = _provider_config()
    cfg.config_path = str(spec_yaml)
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-from-host"


def test_unset_in_both_sources_still_raises_provider_env_error(
    env_save_restore, tmp_path
):
    # Arrange — host env empty, to_home/.env exists but does NOT set the
    # named key. Fail-loud must survive the new fallback path.
    env_save_restore.delete("DEEPSEEK_API_KEY")
    env_save_restore.delete("SAC_TO_HOME_BASELINE")
    spec_yaml = _spec_with_to_home_env(tmp_path, "OTHER_KEY=irrelevant\n")
    cfg = _provider_config()
    cfg.config_path = str(spec_yaml)
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        provider_env_flags(cfg)


def test_to_home_env_supports_quoted_and_export_prefixed_lines(
    env_save_restore, tmp_path
):
    # Arrange — common .env shapes operators copy from dotfiles: an
    # ``export FOO="bar"`` style line with surrounding double quotes.
    env_save_restore.delete("DEEPSEEK_API_KEY")
    env_save_restore.delete("SAC_TO_HOME_BASELINE")
    spec_yaml = _spec_with_to_home_env(
        tmp_path,
        '# comment line\nexport DEEPSEEK_API_KEY="sk-quoted-export"\n',
    )
    cfg = _provider_config()
    cfg.config_path = str(spec_yaml)
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert — quotes stripped, ``export`` prefix tolerated.
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-quoted-export"
