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

from pathlib import Path  # noqa: F401  # used in string annotation on line ~179

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
# scitex-config cascade — $HOME/.env feeds the env layer
# ---------------------------------------------------------------------------
#
# The runtime delegates auth-token resolution to scitex_config:
#   load_dotenv(dotenv_path=$HOME/.env) merges $HOME/.env into os.environ
#   WITHOUT overriding already-set vars, then PriorityConfig.resolve()
#   reads the value from os.environ. The tests below pin $HOME to a
#   tmp_path so the real ~/.env is never touched.


def _write_home_dotenv(home: "Path", body: str) -> None:
    """Write ``$HOME/.env`` under the test's tmp home."""
    (home / ".env").write_text(body)


def test_auth_token_resolved_from_home_dotenv_via_scitex_config(
    env_save_restore, tmp_path
):
    # Arrange — pin HOME to tmp, drop the key in $HOME/.env only.
    # Host env intentionally lacks DEEPSEEK_API_KEY so the value MUST
    # come from the .env via scitex_config.load_dotenv.
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("DEEPSEEK_API_KEY")
    _write_home_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-from-home-dotenv\n")
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert — key bridged from $HOME/.env into SAC_ANTHROPIC_API_KEY
    # through the scitex-config cascade.
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-from-home-dotenv"


def test_shell_export_wins_over_home_dotenv(env_save_restore, tmp_path):
    # Arrange — both layers populated with DIFFERENT values. The shell
    # export should win because scitex_config.load_dotenv never
    # overrides an already-set process env var.
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-from-shell-export")
    _write_home_dotenv(tmp_path, "DEEPSEEK_API_KEY=sk-from-home-dotenv\n")
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-from-shell-export"


def test_unset_in_both_layers_still_raises_provider_env_error(
    env_save_restore, tmp_path
):
    # Arrange — host env empty, $HOME/.env exists but does NOT set the
    # named key. Fail-loud must survive the scitex-config cascade.
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("DEEPSEEK_API_KEY")
    _write_home_dotenv(tmp_path, "OTHER_KEY=irrelevant\n")
    cfg = _provider_config()
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        provider_env_flags(cfg)


def test_home_dotenv_supports_quoted_and_export_prefixed_lines(
    env_save_restore, tmp_path
):
    # Arrange — common .env shapes operators copy from dotfiles: an
    # ``export FOO="bar"`` style line with surrounding double quotes.
    # scitex_config.load_dotenv strips both forms.
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("DEEPSEEK_API_KEY")
    _write_home_dotenv(
        tmp_path,
        '# comment line\nexport DEEPSEEK_API_KEY="sk-quoted-export"\n',
    )
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert — quotes stripped, ``export`` prefix tolerated.
    assert env["SAC_ANTHROPIC_API_KEY"] == "sk-quoted-export"


# ---------------------------------------------------------------------------
# ANTHROPIC_MODEL auto-injection (ADR-0011 extension, lead-learnings/05 fix)
# ---------------------------------------------------------------------------


def test_flags_inject_anthropic_model_from_spec_when_set(env_save_restore):
    # Arrange — provider active and spec.claude.model = "deepseek-chat";
    # auto-injected so the SDK's built-in default doesn't silently win.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-injected")
    cfg = _provider_config()
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert env["ANTHROPIC_MODEL"] == "deepseek-chat"


def test_flags_omit_anthropic_model_when_spec_model_empty(env_save_restore):
    # Arrange — provider active but no model set; SDK's default model
    # is then intentional and should not be overridden by a stray
    # ANTHROPIC_MODEL flag.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-injected")
    cfg = _provider_config()
    cfg.claude.model = ""
    # Act
    env = _env_dict(provider_env_flags(cfg))
    # Assert
    assert "ANTHROPIC_MODEL" not in env
