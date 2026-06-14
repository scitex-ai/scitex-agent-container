"""Tests for ``runtimes._tui_auth_stage.stage_tui_auth``.

Stages the two files the interactive ``claude`` TUI needs into a
materialised ``$HOME``. Lead a2a ``910ff436642948eb85f8b3100204ed9b``
(2026-06-14) — the auth-staging recipe proven by the e2e probe baked
into the runtime so every ``sac agents start`` TUI agent skips the
login picker automatically.

STX-TQ002 AAA markers, STX-TQ007 one-assert. No mocks; sources are
real files under ``tmp_path`` and assertions read real files back.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._tui_auth_stage import (
    CLAUDE_JSON_SRC_ENV,
    CREDENTIALS_SRC_ENV,
    TuiAuthStageError,
    stage_tui_auth,
)

# ---------------------------------------------------------------------------
# Shared fixtures — sandbox $HOME + the two source-path env vars so the
# operator's real ~/.claude.json / /tmp/sac-claude/ is never touched.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path):
    """Save/restore HOME and the two SAC_TUI_AUTH_* env vars."""
    saved: dict[str, str | None] = {
        key: os.environ.get(key)
        for key in ("HOME", CREDENTIALS_SRC_ENV, CLAUDE_JSON_SRC_ENV)
    }
    # Point HOME at a fresh dir so the default ${HOME}/.claude.json
    # source resolves under tmp_path (and is missing by default —
    # tests that exercise the default must pre-stage it themselves).
    os.environ["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    # Unset the env overrides so each test opts in explicitly.
    os.environ.pop(CREDENTIALS_SRC_ENV, None)
    os.environ.pop(CLAUDE_JSON_SRC_ENV, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def creds_src(tmp_path: Path) -> Path:
    """Stage a real ``.credentials.json`` source with realistic content."""
    src = tmp_path / "creds-src" / ".credentials.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-test-token",
                    "refreshToken": "sk-ant-ort01-test-refresh",
                    "expiresAt": 9999999999999,
                    "subscriptionType": "max",
                }
            }
        )
    )
    return src


@pytest.fixture
def claude_json_src(tmp_path: Path) -> Path:
    """Stage a real ``.claude.json`` source with the onboarding block."""
    src = tmp_path / "claude-json-src" / ".claude.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.0.54",
                "userID": "deadbeef" * 8,
                "oauthAccount": {
                    "accountUuid": "11111111-2222-3333-4444-555555555555",
                    "emailAddress": "test@example.com",
                    "organizationUuid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
                    "organizationType": "claude_max",
                },
            }
        )
    )
    return src


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    """Materialised HOME root the TUI runtime would pass."""
    h = tmp_path / "tui-home"
    h.mkdir(parents=True, exist_ok=True)
    return h


@pytest.fixture
def staged_via_env(home_dir: Path, creds_src: Path, claude_json_src: Path) -> None:
    """Stage via env-var overrides — exercises the default code path
    that the TUI runtime uses (defaults pointing at the host)."""
    os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
    os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
    stage_tui_auth(home_dir)


# ---------------------------------------------------------------------------
# Happy path — both sources present, staging lands the right files.
# ---------------------------------------------------------------------------


class TestStageTuiAuthHappyPath:
    """Both sources exist; staging lands real files with correct perms."""

    def test_credentials_file_landed_under_dot_claude(
        self, home_dir: Path, staged_via_env: None
    ) -> None:
        # Arrange
        dst = home_dir / ".claude" / ".credentials.json"
        # Act
        present = dst.is_file()
        # Assert
        assert present is True

    def test_credentials_file_chmod_0600(
        self, home_dir: Path, staged_via_env: None
    ) -> None:
        # Arrange
        dst = home_dir / ".claude" / ".credentials.json"
        # Act
        mode = stat.S_IMODE(dst.stat().st_mode)
        # Assert
        assert mode == 0o600

    def test_credentials_content_matches_source(
        self,
        home_dir: Path,
        creds_src: Path,
        staged_via_env: None,
    ) -> None:
        # Arrange
        dst = home_dir / ".claude" / ".credentials.json"
        # Act
        observed = dst.read_text()
        # Assert
        assert observed == creds_src.read_text()

    def test_claude_json_landed_at_home_root(
        self, home_dir: Path, staged_via_env: None
    ) -> None:
        # Arrange
        dst = home_dir / ".claude.json"
        # Act
        present = dst.is_file()
        # Assert
        assert present is True

    def test_claude_json_content_matches_source(
        self,
        home_dir: Path,
        claude_json_src: Path,
        staged_via_env: None,
    ) -> None:
        # Arrange
        dst = home_dir / ".claude.json"
        # Act
        observed = json.loads(dst.read_text())
        expected = json.loads(claude_json_src.read_text())
        # Assert
        assert observed == expected

    def test_return_value_names_credentials_destination(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        staged = stage_tui_auth(home_dir)
        # Assert
        assert staged.credentials_dst == home_dir / ".claude" / ".credentials.json"

    def test_return_value_names_claude_json_destination(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        staged = stage_tui_auth(home_dir)
        # Assert
        assert staged.claude_json_dst == home_dir / ".claude.json"


# ---------------------------------------------------------------------------
# Fail-loud — missing sources raise TuiAuthStageError.
# ---------------------------------------------------------------------------


class TestStageTuiAuthFailLoud:
    """Missing source → raise with a remedy. No silent fallback."""

    def test_missing_credentials_source_raises(
        self,
        home_dir: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(tmp_path / "nope" / ".credentials.json")
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act / Assert
        with pytest.raises(TuiAuthStageError, match="credentials source missing"):
            stage_tui_auth(home_dir)

    def test_missing_claude_json_source_raises(
        self,
        home_dir: Path,
        creds_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(tmp_path / "nope" / ".claude.json")
        # Act / Assert
        with pytest.raises(TuiAuthStageError, match=".claude.json source missing"):
            stage_tui_auth(home_dir)

    def test_credentials_error_names_env_var(
        self,
        home_dir: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(tmp_path / "nope" / ".credentials.json")
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        with pytest.raises(TuiAuthStageError) as exc_info:
            stage_tui_auth(home_dir)
        # Assert — operator's remedy must include the env var to override.
        assert CREDENTIALS_SRC_ENV in str(exc_info.value)

    def test_claude_json_error_names_env_var(
        self,
        home_dir: Path,
        creds_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(tmp_path / "nope" / ".claude.json")
        # Act
        with pytest.raises(TuiAuthStageError) as exc_info:
            stage_tui_auth(home_dir)
        # Assert
        assert CLAUDE_JSON_SRC_ENV in str(exc_info.value)


# ---------------------------------------------------------------------------
# Defaults — when env not set, falls back to documented paths.
# ---------------------------------------------------------------------------


class TestStageTuiAuthDefaults:
    """When neither env is set, the defaults resolve correctly."""

    def test_default_claude_json_src_is_under_home(
        self,
        home_dir: Path,
        creds_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — stage a .claude.json at ${HOME}/.claude.json so the
        # default resolver finds it; only override the credentials env
        # (the default /tmp/sac-claude/.credentials.json is a real
        # operator-host path we must not touch from a unit test).
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        host_claude_json = Path(os.environ["HOME"]) / ".claude.json"
        host_claude_json.write_text(json.dumps({"hasCompletedOnboarding": True}))
        # Act
        stage_tui_auth(home_dir)
        # Assert — destination materialised from the default-resolved source.
        observed = json.loads((home_dir / ".claude.json").read_text())
        assert observed == {"hasCompletedOnboarding": True}


# ---------------------------------------------------------------------------
# Symlink semantics — sources are followed (cp -Lf).
# ---------------------------------------------------------------------------


class TestStageTuiAuthSymlinkSources:
    """``cp -Lf`` semantics: source symlinks resolve to real content."""

    def test_symlinked_credentials_source_resolves_to_target_content(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — a symlink at the env-pointed path, target is the real file.
        link = tmp_path / "creds-link" / ".credentials.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(creds_src)
        os.environ[CREDENTIALS_SRC_ENV] = str(link)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        stage_tui_auth(home_dir)
        # Assert — dst is a REGULAR FILE with the symlink target's content.
        dst = home_dir / ".claude" / ".credentials.json"
        assert dst.read_text() == creds_src.read_text()

    def test_symlinked_credentials_destination_is_not_a_symlink(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — same shape as above.
        link = tmp_path / "creds-link" / ".credentials.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(creds_src)
        os.environ[CREDENTIALS_SRC_ENV] = str(link)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        stage_tui_auth(home_dir)
        # Assert
        dst = home_dir / ".claude" / ".credentials.json"
        assert dst.is_symlink() is False


# ---------------------------------------------------------------------------
# Idempotency — re-run overwrites previous staging.
# ---------------------------------------------------------------------------


class TestStageTuiAuthIdempotent:
    """Re-running the staging overwrites the prior destination."""

    def test_rerun_overwrites_credentials_content(
        self,
        home_dir: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — two distinct credential sources; stage A then B.
        creds_a = tmp_path / "a" / ".credentials.json"
        creds_a.parent.mkdir(parents=True, exist_ok=True)
        creds_a.write_text('{"version": "A"}')
        creds_b = tmp_path / "b" / ".credentials.json"
        creds_b.parent.mkdir(parents=True, exist_ok=True)
        creds_b.write_text('{"version": "B"}')
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)

        os.environ[CREDENTIALS_SRC_ENV] = str(creds_a)
        stage_tui_auth(home_dir)
        # Act — re-stage with the second source.
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_b)
        stage_tui_auth(home_dir)
        # Assert — destination reflects the LATER source.
        observed = (home_dir / ".claude" / ".credentials.json").read_text()
        assert observed == '{"version": "B"}'


# EOF
