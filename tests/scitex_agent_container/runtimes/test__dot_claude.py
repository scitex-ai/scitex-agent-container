"""Tests for the dot_claude/ deploy + cleanup pipeline.

Replaces test_src_files.py — the legacy ``src_<X>`` sibling-file
convention was retired in favour of a single ``dot_claude/`` directory
next to ``spec.yaml`` (F-DC1). Same invariants are preserved:

  - CLAUDE.md  — marker-protected, user-tail-preserving overwrite
  - .mcp.json  — per-server replace, other servers preserved
  - .env       — full overwrite, mode 0600 (not tested here; trivial)
  - state.md   — full overwrite (not tested here; trivial)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scitex_agent_container.runtimes._dot_claude import (
    END_MARKER,
    _extract_user_tail,
    cleanup_dot_claude,
    deploy_dot_claude,
)

START_MARKER_RE = re.compile(
    r"<!-- Start of scitex-agent-container generated section.*?-->"
)


def _make_config(workdir: str, claude_md_content: str | None = None) -> MagicMock:
    """Build a mock AgentConfig with a dot_claude/ dir next to spec.yaml.

    When ``claude_md_content`` is provided, writes ``dot_claude/CLAUDE.md``
    so the umbrella ``deploy_dot_claude`` will materialize it. Tests that
    exercise other leaf files write them directly under the returned
    ``dot_claude`` directory.
    """
    agent_dir = Path(workdir) / "agent_def"
    dot_dir = agent_dir / "dot_claude"
    dot_dir.mkdir(parents=True, exist_ok=True)
    if claude_md_content is not None:
        (dot_dir / "CLAUDE.md").write_text(claude_md_content)
    cfg = MagicMock()
    cfg.name = "test-agent"
    cfg.labels = {}
    cfg.config_path = str(agent_dir / "spec.yaml")
    # Empty string → auto-discover ``./dot_claude`` next to spec.yaml.
    cfg.dot_claude = ""
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


class TestDeployClaudeMd:
    def test_fresh_deploy_creates_file(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        assert dest.exists()
        content = dest.read_text()
        assert START_MARKER_RE.search(content)
        assert END_MARKER in content
        assert "test-agent" in content

    def test_guide_comment_appended_after_end_marker(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        content = (Path(workdir) / "CLAUDE.md").read_text()
        end_idx = content.index(END_MARKER)
        tail = content[end_idx + len(END_MARKER) :]
        assert "CUSTOM CONTENT" in tail or "custom content" in tail.lower()
        assert "OVERWRITTEN" in tail or "overwritten" in tail.lower()

    def test_user_tail_preserved_across_redeploy(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        existing = dest.read_text()
        dest.write_text(existing + "\n### My Notes\nremember this\n")
        deploy_dot_claude(cfg, workdir)
        assert "remember this" in dest.read_text()

    def test_guide_comment_not_duplicated_on_redeploy(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        deploy_dot_claude(cfg, workdir)
        content = (Path(workdir) / "CLAUDE.md").read_text()
        assert content.count("CUSTOM CONTENT") <= 1

    def test_multiple_start_markers_raises(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        bad = (
            "<!-- Start of scitex-agent-container generated section (ts1) -->\n"
            f"{END_MARKER}\n"
            "<!-- Start of scitex-agent-container generated section (ts2) -->\n"
            f"{END_MARKER}\n"
        )
        (workdir / "CLAUDE.md").write_text(bad)
        with pytest.raises(RuntimeError, match="expected exactly 1"):
            deploy_dot_claude(cfg, str(workdir))

    def test_missing_dot_claude_dir_is_noop(self, tmp_path):
        cfg = MagicMock()
        cfg.name = "ghost"
        cfg.labels = {}
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.dot_claude = ""
        workdir = str(tmp_path / "ws")
        deploy_dot_claude(cfg, workdir)
        assert not (Path(workdir) / "CLAUDE.md").exists()


class TestCleanupStopStartRoundTrip:
    """Regression — the stop→start race that bricked 9 mamba agents on
    MBA on 2026-04-15: cleanup must strip the managed block AND its
    guide comment, otherwise the next deploy's marker validator rejects
    the orphan block."""

    def test_cleanup_then_deploy_does_not_raise(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        cleanup_dot_claude(cfg, workdir)
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        assert dest.exists()
        content = dest.read_text()
        assert START_MARKER_RE.search(content)
        assert END_MARKER in content

    def test_cleanup_removes_file_when_only_managed_section(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        cleanup_dot_claude(cfg, workdir)
        assert not (Path(workdir) / "CLAUDE.md").exists()

    def test_cleanup_preserves_user_tail(self, tmp_path):
        cfg = _make_config(str(tmp_path), SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        dest.write_text(dest.read_text() + "\n### My Notes\nremember this\n")
        cleanup_dot_claude(cfg, workdir)
        assert dest.exists()
        remaining = dest.read_text()
        assert "remember this" in remaining
        assert "Start of scitex-agent-container" not in remaining
        assert "CUSTOM CONTENT" not in remaining

    def test_cleanup_strips_legacy_guide_comment(self, tmp_path):
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
        cleanup_dot_claude(cfg, str(workdir))
        assert not (workdir / "CLAUDE.md").exists()


def _make_mcp_config(tmp_path: Path, servers: dict) -> MagicMock:
    """Build a mock AgentConfig with dot_claude/.mcp.json pre-written."""
    agent_dir = tmp_path / "agent_def"
    dot_dir = agent_dir / "dot_claude"
    dot_dir.mkdir(parents=True, exist_ok=True)
    (dot_dir / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    cfg = MagicMock()
    cfg.name = "test-agent"
    cfg.labels = {}
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.dot_claude = ""
    return cfg


class TestDeployMcpJsonRefresh:
    """Regression — todo#453: workspace .mcp.json must refresh from the
    canonical source on EVERY deploy, not just first launch."""

    def _server_entry(self, channels: str) -> dict:
        return {
            "scitex-orochi": {
                "type": "stdio",
                "command": "bun",
                "args": ["run", "~/proj/scitex-orochi/ts/mcp_channel.ts"],
                "env": {
                    "SCITEX_OROCHI_AGENT": "${metadata.name}",
                    "SCITEX_OROCHI_CHANNELS": channels,
                },
            }
        }

    def test_fresh_deploy_writes_workspace_copy(self, tmp_path):
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(tmp_path, self._server_entry("#ywatanabe,#heads"))
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert (
            data["mcpServers"]["scitex-orochi"]["env"]["SCITEX_OROCHI_CHANNELS"]
            == "#ywatanabe,#heads"
        )

    def test_env_change_propagates_on_redeploy(self, tmp_path):
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(tmp_path, self._server_entry("#ywatanabe,#heads"))
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        assert (
            json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"][
                "SCITEX_OROCHI_CHANNELS"
            ]
            == "#ywatanabe,#heads"
        )

        # Rewrite canonical with extra channels.
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps(
                {"mcpServers": self._server_entry("#ywatanabe,#heads,#lead,#agent")}
            )
        )
        deploy_dot_claude(cfg, workdir)
        assert (
            json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"][
                "SCITEX_OROCHI_CHANNELS"
            ]
            == "#ywatanabe,#heads,#lead,#agent"
        )

    def test_removed_env_key_is_dropped_on_redeploy(self, tmp_path):
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(
            tmp_path,
            {
                "scitex-orochi": {
                    "type": "stdio",
                    "command": "bun",
                    "env": {
                        "SCITEX_OROCHI_AGENT": "${metadata.name}",
                        "LEGACY_KEY": "should-be-dropped",
                    },
                }
            },
        )
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        assert (
            "LEGACY_KEY"
            in json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        )

        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "scitex-orochi": {
                            "type": "stdio",
                            "command": "bun",
                            "env": {"SCITEX_OROCHI_AGENT": "${metadata.name}"},
                        }
                    }
                }
            )
        )
        deploy_dot_claude(cfg, workdir)
        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        assert "LEGACY_KEY" not in env
        assert env["SCITEX_OROCHI_AGENT"] == "test-agent"

    def test_other_servers_preserved(self, tmp_path):
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = _make_mcp_config(tmp_path, self._server_entry("#a,#b"))

        (workdir / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "local-tool": {
                            "type": "stdio",
                            "command": "my-local-mcp",
                        }
                    }
                }
            )
        )
        deploy_dot_claude(cfg, str(workdir))
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "local-tool" in data["mcpServers"]
        assert "scitex-orochi" in data["mcpServers"]

    def test_unconditional_refresh_even_when_src_older(self, tmp_path):
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(tmp_path, self._server_entry("#v2"))
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"

        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": self._server_entry("#v3")}))
        ancient = dest.stat().st_mtime - 3600
        os.utime(src, (ancient, ancient))
        deploy_dot_claude(cfg, workdir)
        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        assert env["SCITEX_OROCHI_CHANNELS"] == "#v3"

    def test_idempotent_when_src_unchanged(self, tmp_path):
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(tmp_path, self._server_entry("#stable"))
        deploy_dot_claude(cfg, workdir)
        first = (Path(workdir) / ".mcp.json").read_text()
        deploy_dot_claude(cfg, workdir)
        second = (Path(workdir) / ".mcp.json").read_text()
        assert first == second
