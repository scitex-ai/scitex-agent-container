"""Tests for the OpenAI SDK-family columns of ``runtimes._apptainer_provider``.

openai-compat-3: when the TOP-LEVEL ``spec.provider`` axis (or the
``SAC_PROVIDER`` ops-only override) resolves to ``openai``, the runtime
injects the OPENAI_* env columns instead of any Anthropic wiring —
``SAC_OPENAI_API_KEY`` + ``OPENAI_API_KEY`` dual injection (key resolved
host-side through the scitex-config cascade, ``SAC_OPENAI_API_KEY``
preferred, matching the in-container precedence of
``runtimes._openai_sdk_common.provision_openai_auth``), a
``SAC_PROVIDER=openai`` marker, and optional ``OPENAI_BASE_URL`` /
``OPENAI_ORG_ID`` / ``OPENAI_PROJECT_ID`` / ``SAC_OPENAI_MODEL``
pass-throughs. Fail-loud (never silent-fallback) when no key resolves
or when composed with an active ``spec.claude.provider`` override.

Real seams only (no mocks): env vars are set / cleared via the
``env_save_restore`` fixture; ``$HOME`` is redirected to a tmp dir so
the real ``~/.env`` can never leak a key into the cascade; configs are
real ``AgentConfig`` dataclasses.

Each test pins one observable fact (TQ007) with AAA markers (TQ002)
and a descriptive name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes._apptainer_provider import (
    ProviderEnvError,
    openai_env_flags,
    openai_provider_active,
    resolve_agent_provider,
)

_OPENAI_ENV_KEYS = (
    "SAC_PROVIDER",
    "SAC_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "SAC_OPENAI_MODEL",
)


@pytest.fixture
def clean_openai_env(tmp_path: Path, env_save_restore):
    """Scrub every OpenAI-column env var and sandbox ``$HOME``.

    ``openai_env_flags`` merges ``$HOME/.env`` into the process env via
    ``scitex_config.load_dotenv``; redirecting ``HOME`` keeps the
    developer's real ``~/.env`` out of the cascade. Pre-deleting every
    column key means any value ``load_dotenv`` re-adds from a test-
    written ``.env`` is tracked and restored by ``env_save_restore``.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    for key in _OPENAI_ENV_KEYS:
        env_save_restore.delete(key)
    return env_save_restore


def _openai_config(name: str = "oai", **claude_kw) -> AgentConfig:
    return AgentConfig(
        name=name,
        runtime="apptainer",
        provider="openai",
        workdir="/tmp/oai-wd",
        claude=ClaudeSpec(**claude_kw),
    )


def _anthropic_config(name: str = "plain") -> AgentConfig:
    return AgentConfig(
        name=name, runtime="apptainer", workdir="/tmp/x", claude=ClaudeSpec()
    )


def _env_dict(flags: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(flags):
        if a == "--env" and i + 1 < len(flags) and "=" in flags[i + 1]:
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Family resolution — spec.provider + the SAC_PROVIDER ops-only override
# ---------------------------------------------------------------------------


def test_openai_provider_active_true_for_openai_spec(clean_openai_env):
    # Arrange
    cfg = _openai_config()
    # Act
    active = openai_provider_active(cfg)
    # Assert
    assert active is True


def test_openai_provider_active_false_for_default_spec(clean_openai_env):
    # Arrange
    cfg = _anthropic_config()
    # Act
    active = openai_provider_active(cfg)
    # Assert
    assert active is False


def test_resolve_agent_provider_defaults_to_anthropic(clean_openai_env):
    # Arrange
    cfg = _anthropic_config()
    # Act
    family = resolve_agent_provider(cfg)
    # Assert
    assert family == "anthropic"


def test_sac_provider_env_flips_default_spec_to_openai(clean_openai_env):
    # Arrange — the ops-only override activates openai without any spec edit.
    clean_openai_env.set("SAC_PROVIDER", "openai")
    cfg = _anthropic_config()
    # Act
    active = openai_provider_active(cfg)
    # Assert
    assert active is True


def test_sac_provider_env_flips_openai_spec_back_to_anthropic(clean_openai_env):
    # Arrange — the override works in both directions (emergency revert).
    clean_openai_env.set("SAC_PROVIDER", "anthropic")
    cfg = _openai_config()
    # Act
    active = openai_provider_active(cfg)
    # Assert
    assert active is False


def test_unknown_sac_provider_value_raises_provider_env_error(clean_openai_env):
    # Arrange — a typo must not silently launch the default family.
    clean_openai_env.set("SAC_PROVIDER", "opnai")
    cfg = _anthropic_config()
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        resolve_agent_provider(cfg)


# ---------------------------------------------------------------------------
# Env injection — happy path
# ---------------------------------------------------------------------------


def test_flags_bridge_host_key_into_sac_openai_api_key(clean_openai_env):
    # Arrange
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["SAC_OPENAI_API_KEY"] == "sk-oai-secret"


def test_flags_mirror_key_into_plain_openai_api_key(clean_openai_env):
    # Arrange — dual injection: consumers without the sac bridge
    # (raw openai clients) read OPENAI_API_KEY directly.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["OPENAI_API_KEY"] == "sk-oai-secret"


def test_flags_mark_resolved_family_via_sac_provider(clean_openai_env):
    # Arrange — the resolved family is observable in-container.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["SAC_PROVIDER"] == "openai"


def test_flags_fall_back_to_plain_openai_api_key(clean_openai_env):
    # Arrange — no sac-tracked key; a pre-existing OPENAI_API_KEY is
    # honoured (matching provision_openai_auth's in-container contract).
    clean_openai_env.set("OPENAI_API_KEY", "sk-plain-key")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["SAC_OPENAI_API_KEY"] == "sk-plain-key"


def test_sac_key_wins_over_plain_openai_api_key(clean_openai_env):
    # Arrange — both set with DIFFERENT values; the sac-tracked source wins.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-sac-tracked")
    clean_openai_env.set("OPENAI_API_KEY", "sk-plain-shadowed")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["OPENAI_API_KEY"] == "sk-sac-tracked"


def test_flags_empty_for_anthropic_family_spec(clean_openai_env):
    # Arrange — a Claude-family agent gets NO OpenAI columns.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _anthropic_config()
    # Act
    flags = openai_env_flags(cfg)
    # Assert
    assert flags == []


# ---------------------------------------------------------------------------
# Optional routing pass-throughs — forwarded only when set on the host
# ---------------------------------------------------------------------------


def test_flags_forward_openai_base_url_when_set(clean_openai_env):
    # Arrange
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    clean_openai_env.set("OPENAI_BASE_URL", "https://gateway.example/v1")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["OPENAI_BASE_URL"] == "https://gateway.example/v1"


def test_flags_omit_openai_base_url_when_unset(clean_openai_env):
    # Arrange — absence means "the SDK default", not an empty override.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert "OPENAI_BASE_URL" not in env


def test_flags_forward_openai_org_id_when_set(clean_openai_env):
    # Arrange
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    clean_openai_env.set("OPENAI_ORG_ID", "org-fleet")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["OPENAI_ORG_ID"] == "org-fleet"


def test_flags_forward_openai_project_id_when_set(clean_openai_env):
    # Arrange
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    clean_openai_env.set("OPENAI_PROJECT_ID", "proj-sac")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["OPENAI_PROJECT_ID"] == "proj-sac"


def test_flags_forward_sac_openai_model_when_set(clean_openai_env):
    # Arrange — the in-container default_openai_model() reads this var;
    # without forwarding, a host export would silently do nothing.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    clean_openai_env.set("SAC_OPENAI_MODEL", "gpt-5.2")
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["SAC_OPENAI_MODEL"] == "gpt-5.2"


# ---------------------------------------------------------------------------
# Fail-loud paths
# ---------------------------------------------------------------------------


def test_missing_key_raises_provider_env_error(clean_openai_env):
    # Arrange — neither SAC_OPENAI_API_KEY nor OPENAI_API_KEY resolves;
    # no silent boot into a 401-every-turn agent.
    cfg = _openai_config()
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        openai_env_flags(cfg)


def test_missing_key_error_names_both_env_vars(clean_openai_env):
    # Arrange
    cfg = _openai_config()
    message = ""
    # Act
    try:
        openai_env_flags(cfg)
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "SAC_OPENAI_API_KEY" in message and "OPENAI_API_KEY" in message


def test_openai_family_with_claude_provider_override_raises(clean_openai_env):
    # Arrange — spec.provider: openai composed with an active
    # spec.claude.provider (Anthropic-compat gateway) is a config error:
    # the nested override configures the Claude SDK, which an
    # openai-family agent never runs.
    clean_openai_env.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    cfg = _openai_config(
        provider=ProviderSpec(
            base_url="https://api.deepseek.com/anthropic",
            auth_token_env="DEEPSEEK_API_KEY",
        )
    )
    # Act
    ctx = pytest.raises(ProviderEnvError)
    # Assert
    with ctx:
        openai_env_flags(cfg)


# ---------------------------------------------------------------------------
# scitex-config cascade — $HOME/.env feeds the env layer
# ---------------------------------------------------------------------------


def test_key_resolved_from_home_dotenv_via_scitex_config(
    clean_openai_env, tmp_path: Path
):
    # Arrange — host env intentionally lacks both key vars; the value
    # MUST come from the sandboxed $HOME/.env via scitex_config.
    (tmp_path / "home" / ".env").write_text(
        "SAC_OPENAI_API_KEY=sk-from-home-dotenv\n"
    )
    cfg = _openai_config()
    # Act
    env = _env_dict(openai_env_flags(cfg))
    # Assert
    assert env["SAC_OPENAI_API_KEY"] == "sk-from-home-dotenv"
