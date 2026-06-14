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
    CREDENTIALS_FALLBACK_CHAIN_ENV,
    CREDENTIALS_SRC_ENV,
    DEFAULT_SETTINGS_FALLBACK,
    SETTINGS_JSON_SRC_ENV,
    TuiAuthStageError,
    stage_tui_auth,
)

# ---------------------------------------------------------------------------
# Shared fixtures — sandbox $HOME + the two source-path env vars so the
# operator's real ~/.claude.json / /tmp/sac-claude/ is never touched.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path):
    """Save/restore HOME, the SAC_TUI_AUTH_* env vars, and the
    credentials fallback-chain override.

    Test isolation note: the dev container ships a real
    ``/tmp/sac-claude/.credentials.json`` (operator's apptainer bind).
    To prevent the production chain from finding it during host-
    fallback tests, the autouse fixture installs a sentinel chain
    pointing at a tmp_path that doesn't exist — every test that
    wants to exercise the real chain unsets the env explicitly.
    """
    saved: dict[str, str | None] = {
        key: os.environ.get(key)
        for key in (
            "HOME",
            CREDENTIALS_SRC_ENV,
            CLAUDE_JSON_SRC_ENV,
            SETTINGS_JSON_SRC_ENV,
            CREDENTIALS_FALLBACK_CHAIN_ENV,
        )
    }
    # Point HOME at a fresh dir so the default ${HOME}/.claude.json
    # source resolves under tmp_path (and is missing by default —
    # tests that exercise the default must pre-stage it themselves).
    os.environ["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    # Unset the env overrides so each test opts in explicitly.
    os.environ.pop(CREDENTIALS_SRC_ENV, None)
    os.environ.pop(CLAUDE_JSON_SRC_ENV, None)
    os.environ.pop(SETTINGS_JSON_SRC_ENV, None)
    # Neutralise the production fallback chain (which would hit the
    # dev container's real /tmp/sac-claude bind) by pointing at a
    # tmp_path entry that doesn't exist. Tests exercising the
    # default chain set it back explicitly.
    os.environ[CREDENTIALS_FALLBACK_CHAIN_ENV] = str(
        tmp_path / "nonexistent" / ".credentials.json"
    )
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
        raised = pytest.raises(TuiAuthStageError, match="credentials source missing")
        # Act
        do_stage = stage_tui_auth
        # Assert
        with raised:
            do_stage(home_dir)

    def test_missing_claude_json_source_raises(
        self,
        home_dir: Path,
        creds_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(tmp_path / "nope" / ".claude.json")
        raised = pytest.raises(TuiAuthStageError, match=".claude.json source missing")
        # Act
        do_stage = stage_tui_auth
        # Assert
        with raised:
            do_stage(home_dir)

    def test_credentials_error_names_env_var(
        self,
        home_dir: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — pytest.raises(match=...) IS the single assertion;
        # matching CREDENTIALS_SRC_ENV proves the error message names
        # the env var the operator can override.
        os.environ[CREDENTIALS_SRC_ENV] = str(tmp_path / "nope" / ".credentials.json")
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        # Act
        do_stage = stage_tui_auth
        # Assert
        with pytest.raises(TuiAuthStageError, match=CREDENTIALS_SRC_ENV):
            do_stage(home_dir)

    def test_claude_json_error_names_env_var(
        self,
        home_dir: Path,
        creds_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — match on the env var name proves the remedy
        # mentions the override; one assertion (pytest.raises match).
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(tmp_path / "nope" / ".claude.json")
        # Act
        do_stage = stage_tui_auth
        # Assert
        with pytest.raises(TuiAuthStageError, match=CLAUDE_JSON_SRC_ENV):
            do_stage(home_dir)


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


# ---------------------------------------------------------------------------
# settings.json — defeats the first-launch theme picker
# ---------------------------------------------------------------------------


class TestStageTuiAuthSettingsFallback:
    """When no settings.json source exists, the fallback is written.

    Covers the dogfood-2026-06-14 finding: an authenticated TUI on a
    fresh HOME wedges on the theme picker. The fallback bypasses it.
    """

    def test_writes_fallback_settings_when_no_source(
        self,
        home_dir: Path,
        staged_via_env: None,
    ) -> None:
        # Arrange — staged_via_env runs with creds+claude.json but no
        # SETTINGS_JSON_SRC_ENV and no ${HOME}/.claude/settings.json,
        # so the fallback path fires.
        path = home_dir / ".claude" / "settings.json"
        # Act
        observed = json.loads(path.read_text())
        # Assert
        assert observed == DEFAULT_SETTINGS_FALLBACK

    def test_returned_path_names_settings_destination(
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
        assert staged.settings_json_dst == home_dir / ".claude" / "settings.json"


class TestStageTuiAuthSettingsHostSource:
    """When ``${HOME}/.claude/settings.json`` exists, it is copied verbatim."""

    def test_host_settings_copied_verbatim(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange — stage a real host settings.json before the call.
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        host_settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
        host_settings.parent.mkdir(parents=True, exist_ok=True)
        host_settings.write_text(json.dumps({"theme": "light", "custom": True}))
        # Act
        stage_tui_auth(home_dir)
        # Assert — destination mirrors the host's settings verbatim.
        observed = json.loads((home_dir / ".claude" / "settings.json").read_text())
        assert observed == {"theme": "light", "custom": True}


class TestStageTuiAuthSettingsEnvOverride:
    """``SAC_TUI_AUTH_SETTINGS_JSON_SRC`` overrides the host default."""

    def test_env_pointed_settings_copied(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — settings.json at an env-pointed location.
        custom = tmp_path / "custom-settings.json"
        custom.write_text(json.dumps({"theme": "custom-dark"}))
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        os.environ[SETTINGS_JSON_SRC_ENV] = str(custom)
        # Act
        stage_tui_auth(home_dir)
        # Assert
        observed = json.loads((home_dir / ".claude" / "settings.json").read_text())
        assert observed == {"theme": "custom-dark"}


class TestStageTuiAuthSettingsPreserveExisting:
    """An existing ``<home>/.claude/settings.json`` (typically written
    by ``deploy_to_home``'s agent overlay) is left untouched — the
    agent's settings win on conflict.
    """

    def test_preexisting_settings_not_overwritten(
        self,
        home_dir: Path,
        creds_src: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange — caller already deployed a settings.json (agent overlay).
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        existing = home_dir / ".claude" / "settings.json"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text(json.dumps({"theme": "agent-overlay-wins"}))
        # Act
        stage_tui_auth(home_dir)
        # Assert
        observed = json.loads(existing.read_text())
        assert observed == {"theme": "agent-overlay-wins"}


# ---------------------------------------------------------------------------
# Pinned-account snapshot — lead a2a 1781e82a (2026-06-14): host starts
# resolve creds from ~/.scitex/agent-container/accounts/<acct>/.credentials.json
# when spec.claude.account is set. Without this, every pinned-account TUI
# agent on a host failed because the default container-bind path
# /tmp/sac-claude does not exist outside apptainer.
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass
class _StubClaude:
    """Real dataclass — no MagicMock. Satisfies the shape
    ``stage_tui_auth`` reads via ``getattr(config.claude, 'account', '')``.
    """

    account: str = ""


@dataclass
class _StubConfig:
    """Minimal AgentConfig surface the resolver touches."""

    claude: _StubClaude


class TestStageTuiAuthPinnedAccountSnapshot:
    """When ``spec.claude.account`` is set AND the snapshot exists,
    the staging copies from the per-account snapshot dir."""

    def test_pinned_account_snapshot_copied_to_dst(
        self,
        home_dir: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange — stage a per-account snapshot under the SAC accounts
        # tree (rooted at ${HOME}/.scitex/agent-container/accounts/).
        acct = "ywata1989"
        snapshot = (
            Path(os.environ["HOME"])
            / ".scitex"
            / "agent-container"
            / "accounts"
            / acct
            / ".credentials.json"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text('{"version": "pinned-snapshot"}')
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        config = _StubConfig(claude=_StubClaude(account=acct))
        # Act
        stage_tui_auth(home_dir, config=config)
        # Assert — destination reflects the snapshot, not the chain default.
        observed = (home_dir / ".claude" / ".credentials.json").read_text()
        assert observed == '{"version": "pinned-snapshot"}'

    def test_pinned_account_with_missing_snapshot_falls_to_chain(
        self,
        home_dir: Path,
        claude_json_src: Path,
        creds_src: Path,
    ) -> None:
        # Arrange — pinned account exists in spec but no snapshot on disk.
        # The resolver falls through to the host live chain entry, which
        # we stage at the env-pointed creds_src for test isolation.
        os.environ[CREDENTIALS_SRC_ENV] = str(creds_src)
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        config = _StubConfig(claude=_StubClaude(account="no-snapshot-here"))
        # Act
        stage_tui_auth(home_dir, config=config)
        # Assert — env override (chain top) wins because no snapshot exists.
        observed = (home_dir / ".claude" / ".credentials.json").read_text()
        assert observed == creds_src.read_text()


class TestStageTuiAuthHostFallbackChain:
    """When neither env nor pinned account resolves, the chain tries
    ``~/.claude/.credentials.json`` after the container bind.

    Lead a2a 1781e82a: this is the host-start path the operator hit
    when /tmp/sac-claude (apptainer bind) was missing.
    """

    def test_host_live_credentials_used_when_container_bind_absent(
        self,
        home_dir: Path,
        claude_json_src: Path,
    ) -> None:
        # Arrange — stage ~/.claude/.credentials.json at the redirected
        # HOME and override the chain so the FIRST entry is absent +
        # SECOND entry is the staged host_live file. Without the chain
        # override we'd hit the dev container's real /tmp/sac-claude
        # bind, which exists and would short-circuit the test.
        host_creds = Path(os.environ["HOME"]) / ".claude" / ".credentials.json"
        host_creds.parent.mkdir(parents=True, exist_ok=True)
        host_creds.write_text('{"version": "host-live"}')
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        os.environ[CREDENTIALS_FALLBACK_CHAIN_ENV] = (
            f"/nope-container-bind/.credentials.json:{host_creds}"
        )
        # Act — config=None means unpinned; chain falls to host_live.
        stage_tui_auth(home_dir, config=None)
        # Assert
        observed = (home_dir / ".claude" / ".credentials.json").read_text()
        assert observed == '{"version": "host-live"}'

    def test_error_lists_all_tried_paths_when_chain_exhausted(
        self,
        home_dir: Path,
        claude_json_src: Path,
        tmp_path: Path,
    ) -> None:
        # Arrange — chain points at two paths that don't exist; no env
        # override, no pinned account. The resolver must exhaust and
        # raise naming "fallback chain" in the message.
        os.environ[CLAUDE_JSON_SRC_ENV] = str(claude_json_src)
        os.environ[CREDENTIALS_FALLBACK_CHAIN_ENV] = (
            f"{tmp_path}/a/.credentials.json:{tmp_path}/b/.credentials.json"
        )
        # Act / Assert
        with pytest.raises(TuiAuthStageError, match="fallback chain"):
            stage_tui_auth(home_dir, config=None)


# EOF
