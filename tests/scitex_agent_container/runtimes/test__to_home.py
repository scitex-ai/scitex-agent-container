"""Tests for the to_home/ materialization pipeline (ADR-0006).

The new layout replaces the leaf-vs-mirror fragmentation of
``dot_claude/``: every path under ``to_home/`` lands at the same
relative path inside ``$HOME``. Marker-protection semantics for
``CLAUDE.md`` / ``state.md`` carry over identically from
:mod:`_dot_claude` — never silent data loss on a hand-edited file.

PA-306 no-mocks: real ``AgentConfig`` instances against ``tmp_path``.
Env-driven tests use the project-wide ``env_save_restore`` fixture
(POSIX-honest equivalent of ``monkeypatch.setenv`` /
``monkeypatch.delenv``).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._dot_claude import END_MARKER
from scitex_agent_container.runtimes._to_home import (
    WorkspaceCLAUDEMarkerError,
    deploy_to_home,
    materialize_to_home,
    resolve_to_home_dir,
)

START_MARKER_RE = re.compile(
    r"<!-- Start of scitex-agent-container generated section.*?-->"
)


# ---------------------------------------------------------------------------
# Real-AgentConfig builders.
# ---------------------------------------------------------------------------


def _build_cfg(
    tmp_path: Path,
    *,
    to_home: str = "",
    labels: dict | None = None,
) -> tuple[AgentConfig, Path]:
    """Build a real ``AgentConfig`` pointing at a ``to_home/`` next to
    ``spec.yaml`` inside ``tmp_path``. Returns (config, to_home_root).
    """
    agent_dir = tmp_path / "agent_def"
    to_home_dir = agent_dir / "to_home"
    to_home_dir.mkdir(parents=True, exist_ok=True)
    cfg = AgentConfig(name="test-agent")
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.to_home = to_home
    if labels:
        cfg.labels = dict(labels)
    return cfg, to_home_dir


# ---------------------------------------------------------------------------
# resolve_to_home_dir — discovery semantics
# ---------------------------------------------------------------------------


class TestResolveToHomeDir:
    def test_auto_discovers_sibling_to_home_dir(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path)
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved == root

    def test_returns_none_when_no_dir_present(self, tmp_path):
        # Arrange — config_path points at a dir without to_home/
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.to_home = ""
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved is None

    def test_honours_explicit_absolute_path_override(self, tmp_path):
        # Arrange
        elsewhere = tmp_path / "elsewhere" / "custom_home"
        elsewhere.mkdir(parents=True)
        cfg, _ = _build_cfg(tmp_path, to_home=str(elsewhere))
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved == elsewhere

    def test_honours_explicit_relative_path_override(self, tmp_path):
        # Arrange — relative path resolves against spec.yaml's dir.
        cfg, _ = _build_cfg(tmp_path, to_home="./to_home")
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert — same as auto-discovery here.
        assert resolved == tmp_path / "agent_def" / "to_home"


# ---------------------------------------------------------------------------
# materialize_to_home — the low-level (no-config) signature.
# ---------------------------------------------------------------------------


class TestMaterializeToHomeBasics:
    def test_missing_to_home_dir_is_noop(self, tmp_path):
        # Arrange — spec_dir with no to_home/ subdir.
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (not home.exists()) or (not any(home.iterdir()))

    def test_plain_file_is_copied_into_home(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".bashrc").write_text("export FOO=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".bashrc").read_text() == "export FOO=1\n"

    def test_directory_structure_is_mirrored(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        nested = spec_dir / "to_home" / ".config" / "gh"
        nested.mkdir(parents=True)
        (nested / "hosts.yml").write_text("github.com:\n  user: ywatanabe\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".config" / "gh" / "hosts.yml").exists()

    def test_nested_directory_content_preserved(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        nested = spec_dir / "to_home" / "secrets"
        nested.mkdir(parents=True)
        (nested / "bot_token").write_text("abcd1234\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "secrets" / "bot_token").read_text() == "abcd1234\n"

    def test_empty_to_home_dir_creates_workspace_home(self, tmp_path):
        # Arrange — empty to_home/.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert home.is_dir()


# ---------------------------------------------------------------------------
# Symlink preservation
# ---------------------------------------------------------------------------


class TestSymlinkPreservation:
    def test_relative_symlink_lands_as_symlink_in_home(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / "real.txt").write_text("payload\n")
        os.symlink("real.txt", spec_dir / "to_home" / "link.txt")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "link.txt").is_symlink()

    def test_relative_symlink_target_string_unchanged(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / "real.txt").write_text("payload\n")
        os.symlink("real.txt", spec_dir / "to_home" / "link.txt")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert os.readlink(home / "link.txt") == "real.txt"

    def test_absolute_symlink_target_string_unchanged(self, tmp_path):
        # Arrange — target outside to_home/ on the host, absolute.
        external = tmp_path / "external_target"
        external.write_text("external\n")
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        os.symlink(str(external), spec_dir / "to_home" / "abs_link")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert os.readlink(home / "abs_link") == str(external)

    def test_broken_symlink_lands_as_symlink_in_home(self, tmp_path):
        # Arrange — link target doesn't exist; we still preserve the link.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        os.symlink("nonexistent.txt", spec_dir / "to_home" / "dead_link")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "dead_link").is_symlink()

    def test_broken_symlink_target_string_unchanged(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        os.symlink("nonexistent.txt", spec_dir / "to_home" / "dead_link")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert os.readlink(home / "dead_link") == "nonexistent.txt"


# ---------------------------------------------------------------------------
# .env tight-perm semantics
# ---------------------------------------------------------------------------


class TestEnvFileChmod:
    def test_env_file_lands_in_home(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("SECRET=xyz\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".env").exists()

    def test_env_file_chmod_is_owner_read_write_only(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("SECRET=xyz\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — only owner read+write (0600).
        mode = stat.S_IMODE((home / ".env").stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Marker-protected merge: CLAUDE.md
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_claude_md_under_home(tmp_path: Path) -> Path:
    """Materialize a single CLAUDE.md under to_home/.claude/ and return
    the resulting destination path. One-shot setup → many single-assert
    tests.
    """
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home" / ".claude").mkdir(parents=True)
    (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
        "## Doctrine\nBe helpful.\n"
    )
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / ".claude" / "CLAUDE.md"


class TestMarkerProtectedClaudeMd:
    def test_fresh_deploy_emits_start_marker(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_fresh_deploy_emits_end_marker(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        # Assert
        assert END_MARKER in content

    def test_fresh_deploy_includes_source_body(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        # Assert
        assert "Be helpful." in content

    def test_redeploy_preserves_user_tail_past_end_marker(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
            "## Doctrine\nBe helpful.\n"
        )
        home = tmp_path / "home"
        materialize_to_home(spec_dir, home)
        dst = home / ".claude" / "CLAUDE.md"
        dst.write_text(dst.read_text() + "\n### My notes\nremember me\n")
        # Act — redeploy.
        materialize_to_home(spec_dir, home)
        # Assert
        assert "remember me" in dst.read_text()

    def test_malformed_existing_markers_hard_abort(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
            "## Doctrine\nBe helpful.\n"
        )
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # Two Start markers — invariant violation.
        bad = (
            "<!-- Start of scitex-agent-container generated section (ts1) -->\n"
            f"{END_MARKER}\n"
            "<!-- Start of scitex-agent-container generated section (ts2) -->\n"
            f"{END_MARKER}\n"
        )
        (home / ".claude" / "CLAUDE.md").write_text(bad)
        # Act
        # Assert
        with pytest.raises(WorkspaceCLAUDEMarkerError, match="expected exactly 1"):
            materialize_to_home(spec_dir, home)


@pytest.fixture
def deployed_state_md(tmp_path: Path) -> Path:
    """Materialize a single state.md under to_home/ and return the path."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    (spec_dir / "to_home" / "state.md").write_text("initial state\n")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / "state.md"


class TestMarkerProtectedStateMd:
    def test_state_md_emits_start_marker(self, deployed_state_md):
        # Arrange
        content = deployed_state_md.read_text()
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_state_md_emits_end_marker(self, deployed_state_md):
        # Arrange
        content = deployed_state_md.read_text()
        # Act
        # Assert
        assert END_MARKER in content


# ---------------------------------------------------------------------------
# deploy_to_home — AgentConfig-driven entrypoint.
# ---------------------------------------------------------------------------


class TestDeployToHomeFromConfig:
    def test_metadata_name_is_interpolated_in_claude_md(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path)
        (root / ".claude").mkdir()
        (root / ".claude" / "CLAUDE.md").write_text("Agent name: ${metadata.name}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "Agent name: test-agent" in (home / ".claude" / "CLAUDE.md").read_text()

    def test_metadata_label_is_interpolated_in_claude_md(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path, labels={"role": "dev"})
        (root / ".claude").mkdir()
        (root / ".claude" / "CLAUDE.md").write_text("Role: ${metadata.labels.role}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "Role: dev" in (home / ".claude" / "CLAUDE.md").read_text()

    def test_env_var_is_interpolated_when_present(self, tmp_path, env_save_restore):
        # Arrange
        env_save_restore.set("MY_TEST_HOST", "spartan-gpgpu180")
        cfg, root = _build_cfg(tmp_path)
        (root / ".bashrc").write_text("export HOST=${MY_TEST_HOST}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "export HOST=spartan-gpgpu180" in (home / ".bashrc").read_text()

    def test_missing_to_home_is_noop_via_deploy_entrypoint(self, tmp_path):
        # Arrange — config_path points at a dir without to_home/.
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (not home.exists()) or (not any(home.iterdir()))

    def test_binary_file_falls_back_to_byte_copy(self, tmp_path):
        # Arrange — embed a binary blob; interpolation must not error.
        cfg, root = _build_cfg(tmp_path)
        payload = bytes(range(256))
        (root / "blob.bin").write_bytes(payload)
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / "blob.bin").read_bytes() == payload
