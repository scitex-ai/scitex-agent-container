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
    resolve_baseline_to_home_dir,
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


# ---------------------------------------------------------------------------
# Baseline layer — shared/common to_home overlaid by per-agent to_home.
#
# Layout under tmp_path:
#   agents/<name>/spec.yaml   ← spec dir
#   agents/<name>/to_home/    ← per-agent layer (overlay, wins on conflict)
#   agents/_base/to_home/     ← shared baseline (applied first)
# ---------------------------------------------------------------------------


def _build_layered(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build the agents-root layout used by baseline tests.

    Returns ``(spec_dir, per_agent_to_home, baseline_to_home)`` — all
    real directories rooted at ``tmp_path/agents``.
    """
    agents_root = tmp_path / "agents"
    spec_dir = agents_root / "test-agent"
    per_agent = spec_dir / "to_home"
    baseline = agents_root / "_base" / "to_home"
    per_agent.mkdir(parents=True, exist_ok=True)
    baseline.mkdir(parents=True, exist_ok=True)
    return spec_dir, per_agent, baseline


class TestResolveBaselineToHomeDir:
    def test_resolves_sibling_base_dir_under_agents_root(self, tmp_path):
        # Arrange
        spec_dir, _, baseline = _build_layered(tmp_path)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == baseline

    def test_returns_none_when_no_base_dir_present(self, tmp_path):
        # Arrange — spec dir without a sibling _base/to_home.
        spec_dir = tmp_path / "agents" / "lonely-agent"
        spec_dir.mkdir(parents=True)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved is None

    def test_returns_none_when_spec_dir_is_none(self):
        # Arrange — no spec dir, no env override.
        # Act
        resolved = resolve_baseline_to_home_dir(None)
        # Assert
        assert resolved is None

    def test_env_override_takes_precedence(self, tmp_path, env_save_restore):
        # Arrange — an explicit baseline elsewhere wins over the sibling.
        spec_dir, _, sibling = _build_layered(tmp_path)
        custom = tmp_path / "custom_baseline" / "to_home"
        custom.mkdir(parents=True)
        env_save_restore.set("SAC_TO_HOME_BASELINE", str(custom))
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == custom

    def test_env_override_pointing_at_missing_dir_returns_none(
        self, tmp_path, env_save_restore
    ):
        # Arrange — override path that does not exist.
        spec_dir, _, _ = _build_layered(tmp_path)
        env_save_restore.set("SAC_TO_HOME_BASELINE", str(tmp_path / "does_not_exist"))
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved is None


class TestMaterializeBaselineOverlay:
    def test_baseline_only_file_lands_in_home(self, tmp_path):
        # Arrange — baseline provides a shared file the agent does not.
        spec_dir, _, baseline = _build_layered(tmp_path)
        (baseline / ".bashrc").write_text("export BASE=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".bashrc").read_text() == "export BASE=1\n"

    def test_per_agent_file_overrides_baseline_file_of_same_name(self, tmp_path):
        # Arrange — same relative path in both layers; per-agent must win.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "shared.txt").write_text("from baseline\n")
        (per_agent / "shared.txt").write_text("from agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "shared.txt").read_text() == "from agent\n"

    def test_distinct_baseline_file_lands_alongside_per_agent(self, tmp_path):
        # Arrange — distinct files in each layer.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "base_only.txt").write_text("base\n")
        (per_agent / "agent_only.txt").write_text("agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "base_only.txt").read_text() == "base\n"

    def test_distinct_per_agent_file_lands_alongside_baseline(self, tmp_path):
        # Arrange — distinct files in each layer.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "base_only.txt").write_text("base\n")
        (per_agent / "agent_only.txt").write_text("agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "agent_only.txt").read_text() == "agent\n"

    def test_absent_baseline_is_unchanged_current_behavior(self, tmp_path):
        # Arrange — no _base/ sibling at all; only a per-agent to_home.
        # (Matches the historical single-layer layout.)
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".bashrc").write_text("export FOO=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — identical to the legacy single-layer test.
        assert (home / ".bashrc").read_text() == "export FOO=1\n"

    def test_baseline_only_with_no_per_agent_to_home(self, tmp_path):
        # Arrange — baseline exists but the agent has NO to_home/ of its own.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        spec_dir.mkdir(parents=True)
        baseline = agents_root / "_base" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / "common.txt").write_text("shared\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "common.txt").read_text() == "shared\n"


class TestDeployBaselineFromConfig:
    def test_baseline_lands_via_config_entrypoint(self, tmp_path):
        # Arrange — config_path under an agents root with a _base sibling.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        (spec_dir / "to_home").mkdir(parents=True)
        baseline = agents_root / "_base" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / ".bashrc").write_text("export BASE=1\n")
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = str(spec_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / ".bashrc").read_text() == "export BASE=1\n"

    def test_per_agent_overrides_baseline_via_config_entrypoint(self, tmp_path):
        # Arrange
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        per_agent = spec_dir / "to_home"
        per_agent.mkdir(parents=True)
        baseline = agents_root / "_base" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / "shared.txt").write_text("from baseline\n")
        (per_agent / "shared.txt").write_text("from agent\n")
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = str(spec_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / "shared.txt").read_text() == "from agent\n"


@pytest.fixture
def overlaid_claude_md(tmp_path: Path) -> Path:
    """Materialize a marker-protected CLAUDE.md from BOTH layers and return
    the destination. Per-agent must win the overlay. One-shot setup feeds
    several single-assert tests.
    """
    spec_dir, per_agent, baseline = _build_layered(tmp_path)
    (baseline / ".claude").mkdir()
    (baseline / ".claude" / "CLAUDE.md").write_text("## Base doctrine\n")
    (per_agent / ".claude").mkdir()
    (per_agent / ".claude" / "CLAUDE.md").write_text("## Agent doctrine\n")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / ".claude" / "CLAUDE.md"


class TestMarkerProtectedOverlay:
    def test_per_agent_body_wins(self, overlaid_claude_md):
        # Arrange
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert "Agent doctrine" in content

    def test_baseline_body_is_overwritten(self, overlaid_claude_md):
        # Arrange
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert "Base doctrine" not in content

    def test_result_has_single_marker_section(self, overlaid_claude_md):
        # Arrange
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert content.count(END_MARKER) == 1


# ---------------------------------------------------------------------------
# Read-only destination overwrite (FIX 2) — hooks are commonly mode 0755 /
# read-only; deploy must overwrite them in place without PermissionError.
# ---------------------------------------------------------------------------


class TestReadOnlyDestinationOverwrite:
    def _deploy_over_readonly(
        self, tmp_path: Path, basename: str, *, mode: int = 0o555
    ) -> Path:
        """Materialize a source file over a pre-existing read-only dest.

        Returns the destination path. Uses the no-config
        ``materialize_to_home`` path so the plain-file ``shutil.copy2``
        branch is exercised directly.
        """
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / basename).write_text("new content\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / basename
        dst.write_text("old content\n")
        os.chmod(dst, mode)
        materialize_to_home(spec_dir, home)
        return dst

    def test_plain_file_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — read-only existing dest (the #142 PermissionError case).
        # Act
        dst = self._deploy_over_readonly(tmp_path, "hook_switch_helper.sh")
        # Assert — no PermissionError raised; content updated.
        assert dst.read_text() == "new content\n"

    def test_plain_file_content_is_updated(self, tmp_path):
        # Arrange
        # Act
        dst = self._deploy_over_readonly(tmp_path, "hook_switch_helper.sh")
        # Assert
        assert "old content" not in dst.read_text()

    def test_marker_protected_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — CLAUDE.md gets marker-protected merge; a prior deploy
        # left a valid marker section that is now read-only.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / "CLAUDE.md").write_text("## doctrine\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / "CLAUDE.md"
        dst.write_text(
            "<!-- Start of scitex-agent-container generated section (old) -->\n"
            "## stale\n"
            f"{END_MARKER}\n"
        )
        os.chmod(dst, 0o444)
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — wrapped section refreshed, no PermissionError.
        assert "doctrine" in dst.read_text()

    def test_tight_perm_file_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — .env gets chmod 0600; dest starts read-only.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("KEY=new\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / ".env"
        dst.write_text("KEY=old\n")
        os.chmod(dst, 0o400)
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert dst.read_text() == "KEY=new\n"


# ---------------------------------------------------------------------------
# Integration: host ``~/.claude/skills/`` resolution is wired into both
# :func:`materialize_to_home` and :func:`deploy_to_home`, and per-agent
# ``to_home/`` still wins on conflict (PR #149 overlay contract).
#
# See ``test__skills_resolve.py`` for the lower-level unit tests of the
# resolver itself; the tests here lock the wiring point so a future
# refactor that drops the resolution from these entrypoints fails loud.
# ---------------------------------------------------------------------------


class TestHostSkillsResolutionWiring:
    def test_materialize_to_home_resolves_host_skills_into_workspace_home(
        self, tmp_path, env_save_restore
    ):
        # Arrange — point the resolver at a real source dir with one
        # symlinked skill; spec_dir has no to_home of its own.
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        proj_source.mkdir(parents=True)
        (proj_source / "SKILL.md").write_text("via materialize\n")
        (host_skills).mkdir()
        os.symlink(str(proj_source), host_skills / "general")
        env_save_restore.set("SAC_HOST_SKILLS_DIR", str(host_skills))
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (
            home / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "via materialize\n"

    def test_deploy_to_home_resolves_host_skills_into_workspace_home(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        proj_source.mkdir(parents=True)
        (proj_source / "SKILL.md").write_text("via deploy\n")
        host_skills.mkdir()
        os.symlink(str(proj_source), host_skills / "general")
        env_save_restore.set("SAC_HOST_SKILLS_DIR", str(host_skills))
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (
            home / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "via deploy\n"

    def test_per_agent_to_home_skill_overrides_host_resolved_skill_on_conflict(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host delivers the "general" skill; per-agent to_home
        # ships its own .claude/skills/general/SKILL.md. Per-agent must
        # win (overlay order locked: host first, then baseline, then
        # per-agent on top).
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        proj_source.mkdir(parents=True)
        (proj_source / "SKILL.md").write_text("from host\n")
        host_skills.mkdir()
        os.symlink(str(proj_source), host_skills / "general")
        env_save_restore.set("SAC_HOST_SKILLS_DIR", str(host_skills))
        cfg, root = _build_cfg(tmp_path)
        agent_skill_md = root / ".claude" / "skills" / "general" / "SKILL.md"
        agent_skill_md.parent.mkdir(parents=True)
        agent_skill_md.write_text("from per-agent to_home\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (
            home / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "from per-agent to_home\n"
