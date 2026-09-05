"""Tests for ``runtimes._apptainer_auth.auth_argv`` (backend wiring).

``auth_argv`` branches on whether ``spec.claude.provider`` is active:

* provider active → API-key backend env (``ANTHROPIC_BASE_URL`` +
  ``SAC_ANTHROPIC_API_KEY`` + a per-agent ``CLAUDE_CONFIG_DIR``), and
  the OAuth ``.credentials.json`` bind is SKIPPED entirely.
* provider absent → the OAuth path: forward host auth env + bind the
  resolved ``.credentials.json`` at ``/tmp/sac-claude``.

Real seams only (no mocks): ``$HOME`` and ``$DEEPSEEK_API_KEY`` are
driven through ``env_save_restore``; a real ``.credentials.json`` file
is written under the redirected home so the OAuth branch has something
to bind.

Each test pins one observable fact (TQ007) with AAA markers (TQ002)
and a descriptive name (TQ003).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes._apptainer_auth import auth_argv


@pytest.fixture
def home_redirect(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so credential resolution stays in the sandbox.

    ``Path.home()`` reads ``$HOME`` on POSIX — no patching needed.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _write_host_creds(home: Path) -> Path:
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    return creds


def _provider_config(workdir: Path, **claude_kw) -> AgentConfig:
    claude = ClaudeSpec(
        model="deepseek-chat",
        provider=ProviderSpec(
            base_url="https://api.deepseek.com/anthropic",
            auth_token_env="DEEPSEEK_API_KEY",
        ),
        **claude_kw,
    )
    return AgentConfig(
        name="ds", runtime="apptainer", workdir=str(workdir), claude=claude
    )


def _oauth_config(workdir: Path) -> AgentConfig:
    return AgentConfig(
        name="oauth-agent",
        runtime="apptainer",
        workdir=str(workdir),
        claude=ClaudeSpec(model="opus"),
    )


# ---------------------------------------------------------------------------
# Provider active → API-key backend, OAuth bind skipped
# ---------------------------------------------------------------------------


def test_provider_argv_injects_base_url(
    tmp_path: Path, home_redirect: Path, env_save_restore
):
    # Arrange — a host creds file exists; the provider path must ignore it.
    _write_host_creds(home_redirect)
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in argv


def test_provider_argv_skips_oauth_credentials_bind(
    tmp_path: Path, home_redirect: Path, env_save_restore
):
    # Arrange — even with a real host creds file present, the OAuth bind
    # must NOT appear when a provider override is active.
    _write_host_creds(home_redirect)
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any("/tmp/sac-claude/.credentials.json" in a for a in argv)


def test_provider_argv_omits_oauth_config_dir(
    tmp_path: Path, home_redirect: Path, env_save_restore
):
    # Arrange — the OAuth CLAUDE_CONFIG_DIR=/tmp/sac-claude must give way
    # to the per-agent provider config dir (the last-wins conflict-breaker).
    _write_host_creds(home_redirect)
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert "CLAUDE_CONFIG_DIR=/tmp/sac-claude" not in argv


def test_provider_argv_binds_the_seeded_config_dir(
    tmp_path: Path, home_redirect: Path, env_save_restore
):
    # Arrange — the per-agent CLAUDE_CONFIG_DIR must be backed by a host dir,
    # or the TUI boots into an empty one and runs first-run onboarding.
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert f"{tmp_path / 'state' / 'provider-cfg'}:/tmp/sac-ds-provider-cfg:rw" in argv


def test_provider_argv_seeds_onboarding_into_that_dir(
    tmp_path: Path, home_redirect: Path, env_save_restore
):
    # Arrange
    env_save_restore.set("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    cfg = _provider_config(tmp_path / "wd")
    # Act
    auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    seeded = json.loads(
        (tmp_path / "state" / "provider-cfg" / ".claude.json").read_text()
    )
    assert seeded["hasCompletedOnboarding"] is True


# ---------------------------------------------------------------------------
# Provider absent → OAuth path unchanged
# ---------------------------------------------------------------------------


def test_oauth_argv_binds_credentials_when_present(tmp_path: Path, home_redirect: Path):
    # Arrange — no provider, real host creds file → OAuth dir-bind emitted.
    # Post task #13 the unpinned branch dir-binds ``~/.claude/`` at
    # ``/tmp/sac-claude`` (same shape as the pinned branch). The bind
    # source is the credentials file's PARENT (``~/.claude/``), not
    # the file itself.
    creds = _write_host_creds(home_redirect)
    cfg = _oauth_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert — dir-bind at /tmp/sac-claude (NOT /tmp/sac-claude/.credentials.json),
    # WRITABLE (shared-credential model, operator 2026-07-11: a rotation
    # performed in-container must be recorded, not silently dropped).
    assert any(a == f"{creds.parent}:/tmp/sac-claude:rw" for a in argv)


def test_oauth_argv_does_not_emit_legacy_file_bind(tmp_path: Path, home_redirect: Path):
    # Arrange — task #13 retired the single-file bind to
    # ``/tmp/sac-claude/.credentials.json``. Make sure it does not slip
    # back in (atomic-rename refreshes orphan the bind → //deleted →
    # 401 at expiry).
    _write_host_creds(home_redirect)
    cfg = _oauth_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert — no bind whose dest is the credentials file path.
    assert not any(":/tmp/sac-claude/.credentials.json:" in a for a in argv)


def test_oauth_argv_sets_oauth_config_dir(tmp_path: Path, home_redirect: Path):
    # Arrange
    _write_host_creds(home_redirect)
    cfg = _oauth_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert "CLAUDE_CONFIG_DIR=/tmp/sac-claude" in argv


def test_oauth_argv_omits_provider_base_url(tmp_path: Path, home_redirect: Path):
    # Arrange — no provider means no ANTHROPIC_BASE_URL injection.
    _write_host_creds(home_redirect)
    cfg = _oauth_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any(a.startswith("ANTHROPIC_BASE_URL=") for a in argv)


# ---------------------------------------------------------------------------
# spec.provider: openai (TOP-LEVEL family axis) → OPENAI columns only
# (openai-compat-3; see _apptainer_provider.openai_env_flags)
# ---------------------------------------------------------------------------


@pytest.fixture
def openai_family_env(home_redirect: Path, env_save_restore):
    """Scrub the family override + key vars, then install a fake sac key.

    ``home_redirect`` already sandboxes ``$HOME`` so the real ``~/.env``
    never feeds ``openai_env_flags``' scitex-config cascade.
    """
    for key in ("SAC_PROVIDER", "SAC_OPENAI_API_KEY", "OPENAI_API_KEY"):
        env_save_restore.delete(key)
    env_save_restore.set("SAC_OPENAI_API_KEY", "sk-oai-secret")
    return env_save_restore


def _openai_family_config(workdir: Path) -> AgentConfig:
    return AgentConfig(
        name="oai-agent",
        runtime="apptainer",
        harness="openai",
        workdir=str(workdir),
        claude=ClaudeSpec(),
    )


def test_openai_family_argv_injects_sac_openai_api_key(
    tmp_path: Path, home_redirect: Path, openai_family_env
):
    # Arrange
    cfg = _openai_family_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert "SAC_OPENAI_API_KEY=sk-oai-secret" in argv


def test_openai_family_argv_skips_oauth_credentials_bind(
    tmp_path: Path, home_redirect: Path, openai_family_env
):
    # Arrange — even with a real host creds file present, an openai-family
    # launch must not bind any Anthropic credential.
    _write_host_creds(home_redirect)
    cfg = _openai_family_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any("/tmp/sac-claude" in a for a in argv)


def test_openai_family_argv_omits_claude_config_dir(
    tmp_path: Path, home_redirect: Path, openai_family_env
):
    # Arrange — no Claude CLI/SDK runs, so no config-dir redirect either.
    _write_host_creds(home_redirect)
    cfg = _openai_family_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any(a.startswith("CLAUDE_CONFIG_DIR=") for a in argv)


def test_openai_family_argv_does_not_forward_anthropic_key(
    tmp_path: Path, home_redirect: Path, openai_family_env
):
    # Arrange — a host ANTHROPIC_API_KEY must NOT leak into an
    # openai-family container (the OAuth forwarding path is skipped).
    openai_family_env.set("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    cfg = _openai_family_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any(a.startswith("ANTHROPIC_API_KEY=") for a in argv)


def test_default_family_argv_omits_openai_key(
    tmp_path: Path, home_redirect: Path, openai_family_env
):
    # Arrange — the Claude column stays clean: a default (anthropic)
    # spec never receives the OPENAI columns even when the host holds
    # an OpenAI key.
    _write_host_creds(home_redirect)
    cfg = _oauth_config(tmp_path / "wd")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert not any(a.startswith("SAC_OPENAI_API_KEY=") for a in argv)
