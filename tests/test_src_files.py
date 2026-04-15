"""Tests for deploy_src_claude_md workspace CLAUDE.md deployment logic."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scitex_agent_container.runtimes.src_files import (
    END_MARKER,
    _extract_user_tail,
    cleanup_src_claude_md,
    deploy_src_claude_md,
)

START_MARKER_RE = re.compile(
    r"<!-- Start of scitex-agent-container generated section.*?-->"
)


def _make_config(workdir: str, src_content: str) -> MagicMock:
    """Create a mock AgentConfig whose src_CLAUDE.md lives in a tmp dir."""
    agent_dir = Path(workdir) / "agent_def"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "src_CLAUDE.md").write_text(src_content)
    cfg = MagicMock()
    cfg.name = "test-agent"
    cfg.labels = {}
    cfg.config_path = str(agent_dir / "test-agent.yaml")
    return cfg


SRC_BODY = "## Agent: test-agent\n\n### Role\nTest agent.\n"


class TestExtractUserTail:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert _extract_user_tail(tmp_path / "nonexistent.md") == ""

    def test_returns_empty_when_marker_absent(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("no markers here\n")
        assert _extract_user_tail(f) == ""

    def test_returns_tail_after_marker(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text(f"generated\n{END_MARKER}\nmy notes\n")
        assert _extract_user_tail(f) == "\nmy notes\n"

    def test_uses_last_marker_occurrence(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text(f"{END_MARKER}\nold\n{END_MARKER}\nnew tail\n")
        assert _extract_user_tail(f) == "\nnew tail\n"


class TestDeploySrcClaudeMd:
    def test_fresh_deploy_creates_file(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        assert dest.exists()
        content = dest.read_text()
        assert START_MARKER_RE.search(content)
        assert END_MARKER in content
        assert "test-agent" in content

    def test_guide_comment_appended_after_end_marker(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        content = (Path(workdir) / "CLAUDE.md").read_text()
        end_idx = content.index(END_MARKER)
        tail = content[end_idx + len(END_MARKER):]
        # Guide comment should mention custom content and overwrite warning
        assert "CUSTOM CONTENT" in tail or "custom content" in tail.lower()
        assert "OVERWRITTEN" in tail or "overwritten" in tail.lower()

    def test_user_tail_preserved_across_redeploy(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        # Simulate agent writing notes below the end marker
        existing = dest.read_text()
        dest.write_text(existing + "\n### My Notes\nremember this\n")
        # Re-deploy (e.g., agent restart)
        deploy_src_claude_md(cfg, workdir)
        updated = dest.read_text()
        assert "remember this" in updated

    def test_guide_comment_not_duplicated_on_redeploy(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        deploy_src_claude_md(cfg, workdir)
        content = (Path(workdir) / "CLAUDE.md").read_text()
        assert content.count("CUSTOM CONTENT") <= 1

    def test_multiple_start_markers_raises(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        dest = Path(workdir)
        dest.mkdir(parents=True, exist_ok=True)
        bad = (
            "<!-- Start of scitex-agent-container generated section (ts1) -->\n"
            f"{END_MARKER}\n"
            "<!-- Start of scitex-agent-container generated section (ts2) -->\n"
            f"{END_MARKER}\n"
        )
        (dest / "CLAUDE.md").write_text(bad)
        with pytest.raises(RuntimeError, match="expected exactly 1"):
            deploy_src_claude_md(cfg, str(dest))

    def test_src_file_missing_is_noop(self, tmp_path):
        cfg = MagicMock()
        cfg.name = "ghost"
        cfg.labels = {}
        cfg.config_path = str(tmp_path / "ghost" / "ghost.yaml")
        workdir = str(tmp_path / "ws")
        deploy_src_claude_md(cfg, workdir)
        assert not (Path(workdir) / "CLAUDE.md").exists()


class TestCleanupStopStartRoundTrip:
    """Regression tests for the stop -> start race that bricked 9 mamba
    agents on MBA on 2026-04-15 (head-mba msg#12607, msg#12615).

    The earlier cleanup regex looked for a legacy ``↓ Your custom content``
    guide comment, but deploy_src_claude_md now emits a ``====`` framed
    "CUSTOM CONTENT — edit freely" guide comment. On stop, the regex
    failed to strip the guide comment, leaving an orphan ~7-line block
    in the workspace CLAUDE.md. On the next start, the marker validator
    saw a non-empty file with zero Start/End markers and hard-aborted
    with WorkspaceCLAUDEMarkerError — breaking every restart.
    """

    def test_cleanup_then_deploy_does_not_raise(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        cleanup_src_claude_md(cfg, workdir)
        # Must not raise WorkspaceCLAUDEMarkerError on the subsequent deploy
        deploy_src_claude_md(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        assert dest.exists()
        content = dest.read_text()
        assert START_MARKER_RE.search(content)
        assert END_MARKER in content

    def test_cleanup_removes_file_when_only_managed_section(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        cleanup_src_claude_md(cfg, workdir)
        # Nothing was written below the End marker, so cleanup should
        # remove the file entirely rather than leave an orphan guide
        # comment that the next deploy's validator would reject.
        assert not (Path(workdir) / "CLAUDE.md").exists()

    def test_cleanup_preserves_user_tail(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_src_claude_md(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        dest.write_text(dest.read_text() + "\n### My Notes\nremember this\n")
        cleanup_src_claude_md(cfg, workdir)
        # File should still exist with the user tail, minus the managed
        # block and its guide comment.
        assert dest.exists()
        remaining = dest.read_text()
        assert "remember this" in remaining
        assert "Start of scitex-agent-container" not in remaining
        assert "CUSTOM CONTENT" not in remaining

    def test_cleanup_strips_legacy_guide_comment(self, tmp_path):
        """Legacy workspaces may still carry the old ``↓ Your custom
        content`` guide comment from earlier versions. Cleanup must
        strip it along with the managed block.
        """
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        legacy = (
            "<!-- Start of scitex-agent-container generated section (old) -->\n"
            "body\n"
            f"{END_MARKER}\n"
            "<!-- ↓ Your custom content goes here -->\n"
        )
        (workdir / "CLAUDE.md").write_text(legacy)
        cfg = _make_config(str(tmp_path), SRC_BODY)
        cleanup_src_claude_md(cfg, str(workdir))
        # Nothing survives the strip, so the file is removed.
        assert not (workdir / "CLAUDE.md").exists()
