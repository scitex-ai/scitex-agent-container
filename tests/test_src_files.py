"""Tests for deploy_src_claude_md workspace CLAUDE.md deployment logic."""

from __future__ import annotations

import json
import os
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
    deploy_src_mcp_json,
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


def _make_mcp_config(defdir: Path, servers: dict) -> MagicMock:
    """Create a mock AgentConfig pointing at a tmp dir with src_mcp.json."""
    defdir.mkdir(parents=True, exist_ok=True)
    (defdir / "src_mcp.json").write_text(json.dumps({"mcpServers": servers}))
    cfg = MagicMock()
    cfg.name = "test-agent"
    cfg.labels = {}
    cfg.config_path = str(defdir / "test-agent.yaml")
    return cfg


class TestDeploySrcMcpJsonRefresh:
    """Regression tests for todo#453 — workspace .mcp.json must refresh
    from canonical src_mcp.json on EVERY deploy, not just first launch.

    The 2026-04-15 fleet-lead incident: canonical ``src_mcp.json`` was
    updated (PR #49 added ``#lead`` and ``#agent`` to the channel
    subscription), but because the workspace ``.mcp.json`` had been
    copied on first launch 2 hours prior, the running agent kept
    reading the stale two-channel subscription. Every agent restart
    must re-read src_mcp.json and propagate canonical changes.
    """

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
        defdir = tmp_path / "def"
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(defdir, self._server_entry("#ywatanabe,#heads"))

        deploy_src_mcp_json(cfg, workdir)

        dest = Path(workdir) / ".mcp.json"
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert data["mcpServers"]["scitex-orochi"]["env"][
            "SCITEX_OROCHI_CHANNELS"
        ] == "#ywatanabe,#heads"

    def test_env_change_propagates_on_redeploy(self, tmp_path):
        """The todo#453 scenario exactly: canonical src_mcp.json gets a
        new channel added after first launch; the next deploy must
        overwrite the stale workspace env.
        """
        defdir = tmp_path / "def"
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(defdir, self._server_entry("#ywatanabe,#heads"))

        # First launch: workspace gets old subscription
        deploy_src_mcp_json(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        assert json.loads(dest.read_text())["mcpServers"]["scitex-orochi"][
            "env"
        ]["SCITEX_OROCHI_CHANNELS"] == "#ywatanabe,#heads"

        # Canonical PR lands: new channels appear in src_mcp.json
        (defdir / "src_mcp.json").write_text(
            json.dumps(
                {"mcpServers": self._server_entry(
                    "#ywatanabe,#heads,#lead,#agent"
                )}
            )
        )

        # Restart: deploy re-runs, workspace MUST reflect canonical
        deploy_src_mcp_json(cfg, workdir)

        refreshed = json.loads(dest.read_text())
        assert refreshed["mcpServers"]["scitex-orochi"]["env"][
            "SCITEX_OROCHI_CHANNELS"
        ] == "#ywatanabe,#heads,#lead,#agent"

    def test_removed_env_key_is_dropped_on_redeploy(self, tmp_path):
        """Per-server replace semantics: if src drops an env key, the
        workspace copy must also drop it. This guards against the
        inverse stale-state bug (a retired env var lingering).
        """
        defdir = tmp_path / "def"
        workdir = str(tmp_path / "workspace")

        server_with_extra = {
            "scitex-orochi": {
                "type": "stdio",
                "command": "bun",
                "env": {
                    "SCITEX_OROCHI_AGENT": "${metadata.name}",
                    "LEGACY_KEY": "should-be-dropped",
                },
            }
        }
        (defdir).mkdir(parents=True, exist_ok=True)
        (defdir / "src_mcp.json").write_text(
            json.dumps({"mcpServers": server_with_extra})
        )
        cfg = MagicMock()
        cfg.name = "test-agent"
        cfg.labels = {}
        cfg.config_path = str(defdir / "test-agent.yaml")

        deploy_src_mcp_json(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        assert "LEGACY_KEY" in json.loads(dest.read_text())[
            "mcpServers"
        ]["scitex-orochi"]["env"]

        # Canonical drops the key
        server_no_extra = {
            "scitex-orochi": {
                "type": "stdio",
                "command": "bun",
                "env": {"SCITEX_OROCHI_AGENT": "${metadata.name}"},
            }
        }
        (defdir / "src_mcp.json").write_text(
            json.dumps({"mcpServers": server_no_extra})
        )

        deploy_src_mcp_json(cfg, workdir)

        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        assert "LEGACY_KEY" not in env
        assert env["SCITEX_OROCHI_AGENT"] == "test-agent"

    def test_other_servers_preserved(self, tmp_path):
        """Servers present in workspace .mcp.json but NOT declared by this
        agent's src_mcp.json are preserved (user-added local tools etc.).
        """
        defdir = tmp_path / "def"
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = _make_mcp_config(defdir, self._server_entry("#a,#b"))

        # Pre-seed workspace with a foreign server
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

        deploy_src_mcp_json(cfg, str(workdir))

        data = json.loads((workdir / ".mcp.json").read_text())
        assert "local-tool" in data["mcpServers"]
        assert "scitex-orochi" in data["mcpServers"]

    def test_unconditional_refresh_even_when_src_older(self, tmp_path):
        """The invariant is unconditional: we refresh even if the src
        mtime is older than the workspace copy. This protects against
        clock-skew or filesystem-copy edge cases where the canonical
        file's mtime might be artificially earlier than the workspace.

        Without this guarantee, a naive ``if src.mtime > dest.mtime``
        fast-path would silently skip legitimate updates.
        """
        defdir = tmp_path / "def"
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(defdir, self._server_entry("#v2"))

        deploy_src_mcp_json(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"

        # Rewrite src with new content, but backdate its mtime so it
        # appears OLDER than the workspace copy.
        (defdir / "src_mcp.json").write_text(
            json.dumps({"mcpServers": self._server_entry("#v3")})
        )
        ancient = dest.stat().st_mtime - 3600  # 1 hour older
        os.utime(defdir / "src_mcp.json", (ancient, ancient))

        deploy_src_mcp_json(cfg, workdir)

        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"][
            "env"
        ]
        assert env["SCITEX_OROCHI_CHANNELS"] == "#v3"

    def test_idempotent_when_src_unchanged(self, tmp_path):
        """Repeated deploys with no src change produce byte-identical
        output."""
        defdir = tmp_path / "def"
        workdir = str(tmp_path / "workspace")
        cfg = _make_mcp_config(defdir, self._server_entry("#stable"))

        deploy_src_mcp_json(cfg, workdir)
        first = (Path(workdir) / ".mcp.json").read_text()

        deploy_src_mcp_json(cfg, workdir)
        second = (Path(workdir) / ".mcp.json").read_text()

        assert first == second
