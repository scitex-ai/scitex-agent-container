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
    # Assert — dir-bind at /tmp/sac-claude (NOT /tmp/sac-claude/.credentials.json).
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
