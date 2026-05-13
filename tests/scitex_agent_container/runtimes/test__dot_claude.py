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


# ---------------------------------------------------------------------------
# Merged from test__dot_claude_extras.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock

from scitex_agent_container.runtimes import _dot_claude as dc
from scitex_agent_container.runtimes._dot_claude import (
    _interpolate_env,
    _interpolate_metadata,
    _iter_extras,
    cleanup_dot_claude,
    deploy_dot_claude,
    resolve_dot_claude_dir,
)


def _make_cfg(tmp_path: Path, *, dot_claude: str = "") -> MagicMock:
    agent_dir = tmp_path / "agent_def"
    (agent_dir / "dot_claude").mkdir(parents=True, exist_ok=True)
    cfg = MagicMock()
    cfg.name = "test-agent"
    cfg.labels = {"role": "head"}
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.dot_claude = dot_claude
    return cfg


# --- resolve_dot_claude_dir branches --------------------------------------


class TestResolveDotClaudeDir:
    def test_returns_none_when_config_path_missing(self):
        cfg = MagicMock()
        cfg.config_path = None
        cfg.dot_claude = ""
        assert resolve_dot_claude_dir(cfg) is None

    def test_relative_path_resolves_against_spec_dir(self, tmp_path):
        custom = tmp_path / "agent_def" / "my_dot"
        custom.mkdir(parents=True)
        cfg = _make_cfg(tmp_path, dot_claude="my_dot")
        result = resolve_dot_claude_dir(cfg)
        assert result == custom

    def test_absolute_path_used_directly(self, tmp_path):
        custom = tmp_path / "abs_dot"
        custom.mkdir()
        cfg = _make_cfg(tmp_path, dot_claude=str(custom))
        assert resolve_dot_claude_dir(cfg) == custom

    def test_relative_path_without_spec_dir_returns_none(self):
        cfg = MagicMock()
        cfg.config_path = None
        cfg.dot_claude = "relative_dir"
        assert resolve_dot_claude_dir(cfg) is None

    def test_nonexistent_path_returns_none(self, tmp_path):
        cfg = _make_cfg(tmp_path, dot_claude=str(tmp_path / "does_not_exist"))
        assert resolve_dot_claude_dir(cfg) is None


# --- interpolation helpers -------------------------------------------------


class TestInterpolation:
    def test_metadata_name_substitution(self):
        cfg = MagicMock()
        cfg.name = "myagent"
        cfg.labels = {}
        out = _interpolate_metadata("hello ${metadata.name}", cfg)
        assert out == "hello myagent"

    def test_metadata_labels_substitution(self):
        cfg = MagicMock()
        cfg.name = "x"
        cfg.labels = {"role": "head"}
        assert _interpolate_metadata("R=${metadata.labels.role}", cfg) == "R=head"

    def test_metadata_unknown_label_left_in_place(self):
        cfg = MagicMock()
        cfg.name = "x"
        cfg.labels = {}
        original = "X=${metadata.labels.missing}"
        assert _interpolate_metadata(original, cfg) == original

    def test_metadata_unknown_key_left_in_place(self):
        cfg = MagicMock()
        cfg.name = "x"
        cfg.labels = {}
        original = "X=${something.else}"
        assert _interpolate_metadata(original, cfg) == original

    def test_env_substitution(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "VALUE")
        assert _interpolate_env("X=${MY_TEST_VAR}") == "X=VALUE"

    def test_env_missing_left_in_place(self, monkeypatch):
        monkeypatch.delenv("NEVER_SET_VAR_XYZ", raising=False)
        assert _interpolate_env("X=${NEVER_SET_VAR_XYZ}") == "X=${NEVER_SET_VAR_XYZ}"


# --- .env deploy/cleanup ---------------------------------------------------


class TestDeployEnv:
    def test_deploy_writes_file_with_mode_600(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text(
            "API_KEY=abc\nNAME=${metadata.name}\n"
        )
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        dest = workdir / ".env"
        assert dest.exists()
        text = dest.read_text()
        assert "API_KEY=abc" in text
        assert "NAME=test-agent" in text
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode == 0o600

    def test_empty_env_is_skipped(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("   \n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert not (workdir / ".env").exists()

    def test_appends_trailing_newline(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("X=1")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert (workdir / ".env").read_text().endswith("\n")

    def test_chmod_failure_is_warned_not_raised(self, tmp_path, monkeypatch, caplog):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("X=1\n")
        workdir = tmp_path / "ws"

        def _boom(path, mode):
            raise OSError("simulated chmod failure")

        monkeypatch.setattr(dc.os, "chmod", _boom)
        import logging

        with caplog.at_level(logging.WARNING):
            deploy_dot_claude(cfg, str(workdir))
        assert (workdir / ".env").exists()
        assert any("chmod" in r.getMessage() for r in caplog.records)

    def test_cleanup_env_removes_workspace_copy(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("X=1\n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert (workdir / ".env").exists()
        cleanup_dot_claude(cfg, str(workdir))
        assert not (workdir / ".env").exists()

    def test_cleanup_env_noop_when_src_absent(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".env").write_text("X=1\n")
        cleanup_dot_claude(cfg, str(workdir))
        # No src .env → cleanup does NOT remove workspace .env
        assert (workdir / ".env").exists()


# --- state.md deploy/cleanup ----------------------------------------------


class TestDeployStateMd:
    def test_deploy_writes_with_substitution(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text(
            "# State for ${metadata.name}"
        )
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        text = (workdir / "state.md").read_text()
        assert "test-agent" in text
        assert text.endswith("\n")

    def test_empty_state_skipped(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text("   \n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert not (workdir / "state.md").exists()

    def test_cleanup_removes_state_md(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text("# s\n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        cleanup_dot_claude(cfg, str(workdir))
        assert not (workdir / "state.md").exists()

    def test_cleanup_noop_when_state_md_missing(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        workdir = tmp_path / "ws"
        workdir.mkdir()
        cleanup_dot_claude(cfg, str(workdir))  # no raise


# --- extras (mirror everything else under .claude/) ------------------------


class TestExtras:
    def test_iter_extras_skips_known_root_files(self, tmp_path):
        root = tmp_path / "dot"
        root.mkdir()
        (root / "CLAUDE.md").write_text("x")
        (root / ".env").write_text("x")
        (root / ".mcp.json").write_text("x")
        (root / "state.md").write_text("x")
        (root / "commands").mkdir()
        (root / "skills").mkdir()
        names = sorted(c.name for c in _iter_extras(root))
        assert names == ["commands", "skills"]

    def test_iter_extras_skips_ds_store(self, tmp_path):
        root = tmp_path / "dot"
        root.mkdir()
        (root / ".DS_Store").write_text("")
        (root / "hooks").mkdir()
        names = [c.name for c in _iter_extras(root)]
        assert ".DS_Store" not in names
        assert "hooks" in names

    def test_iter_extras_noop_when_root_missing(self, tmp_path):
        assert list(_iter_extras(tmp_path / "nope")) == []

    def test_deploy_mirrors_commands_skills_hooks(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        root = tmp_path / "agent_def" / "dot_claude"
        (root / "commands").mkdir()
        (root / "commands" / "hi.md").write_text("hi")
        (root / "skills").mkdir()
        (root / "skills" / "do.md").write_text("do")
        (root / "hooks").mkdir()
        (root / "hooks" / "h.sh").write_text("#!/bin/sh\n")
        (root / "loose.md").write_text("loose")

        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        claude = workdir / ".claude"
        assert (claude / "commands" / "hi.md").read_text() == "hi"
        assert (claude / "skills" / "do.md").read_text() == "do"
        assert (claude / "hooks" / "h.sh").read_text() == "#!/bin/sh\n"
        assert (claude / "loose.md").read_text() == "loose"

    def test_deploy_extras_overwrites_existing_dir(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        root = tmp_path / "agent_def" / "dot_claude"
        (root / "commands").mkdir()
        (root / "commands" / "new.md").write_text("new")

        workdir = tmp_path / "ws"
        # Pre-existing stale content
        stale = workdir / ".claude" / "commands"
        stale.mkdir(parents=True)
        (stale / "stale.md").write_text("stale")

        deploy_dot_claude(cfg, str(workdir))
        cmds = workdir / ".claude" / "commands"
        assert (cmds / "new.md").exists()
        assert not (cmds / "stale.md").exists()

    def test_cleanup_extras_removes_mirrored_entries(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        root = tmp_path / "agent_def" / "dot_claude"
        (root / "commands").mkdir()
        (root / "commands" / "x.md").write_text("x")
        (root / "loose.md").write_text("loose")

        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert (workdir / ".claude" / "commands").exists()
        cleanup_dot_claude(cfg, str(workdir))
        assert not (workdir / ".claude" / "commands").exists()
        assert not (workdir / ".claude" / "loose.md").exists()

    def test_cleanup_extras_noop_when_claude_dir_missing(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "commands").mkdir()
        workdir = tmp_path / "ws"
        workdir.mkdir()
        # No .claude/ in workdir — should not raise
        cleanup_dot_claude(cfg, str(workdir))


# --- malformed-JSON tolerance in .mcp.json ---------------------------------


class TestMcpJsonRobustness:
    def test_src_malformed_json_is_skipped(self, tmp_path, caplog):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text("{not json")
        workdir = tmp_path / "ws"
        import logging

        with caplog.at_level(logging.WARNING):
            deploy_dot_claude(cfg, str(workdir))
        assert not (workdir / ".mcp.json").exists()
        assert any("Invalid JSON" in r.getMessage() for r in caplog.records)

    def test_src_empty_mcp_json_is_skipped(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text("  \n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert not (workdir / ".mcp.json").exists()

    def test_existing_workspace_malformed_json_treated_as_empty(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}})
        )
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text("broken{")
        deploy_dot_claude(cfg, str(workdir))
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "a" in data["mcpServers"]

    def test_existing_workspace_list_treated_as_empty(self, tmp_path):
        """If dest .mcp.json is a JSON array (not dict), drop it."""
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}})
        )
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(json.dumps(["array", "form"]))
        deploy_dot_claude(cfg, str(workdir))
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "a" in data["mcpServers"]

    def test_tilde_in_args_is_expanded(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "tool": {
                            "command": "bun",
                            "args": ["run", "~/script.ts", "--flag"],
                        }
                    }
                }
            )
        )
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        args = json.loads((workdir / ".mcp.json").read_text())["mcpServers"]["tool"][
            "args"
        ]
        assert args[0] == "run"
        assert not args[1].startswith("~")
        assert args[1].endswith("script.ts")
        assert args[2] == "--flag"


# --- cleanup_mcp_json edge cases -------------------------------------------


class TestCleanupMcpJson:
    def test_cleanup_removes_only_managed_servers(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"mng": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "mng": {"command": "x"},
                        "user-local": {"command": "y"},
                    }
                }
            )
        )
        cleanup_dot_claude(cfg, str(workdir))
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "mng" not in data["mcpServers"]
        assert "user-local" in data["mcpServers"]

    def test_cleanup_removes_file_when_empty_after(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"only": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"only": {"command": "x"}}})
        )
        cleanup_dot_claude(cfg, str(workdir))
        assert not (workdir / ".mcp.json").exists()

    def test_cleanup_noop_when_src_malformed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text("not json")
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"keep": {"command": "x"}}})
        )
        cleanup_dot_claude(cfg, str(workdir))
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "keep" in data["mcpServers"]

    def test_cleanup_noop_when_src_has_no_servers(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"keep": {"command": "x"}}})
        )
        cleanup_dot_claude(cfg, str(workdir))
        # No servers to remove → workspace unchanged
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "keep" in data["mcpServers"]

    def test_cleanup_noop_when_dest_missing(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        cleanup_dot_claude(cfg, str(workdir))  # no raise

    def test_cleanup_noop_when_dest_malformed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text("not json {")
        cleanup_dot_claude(cfg, str(workdir))
        # Should not raise; file may stay as-is
        assert (workdir / ".mcp.json").exists()


# --- CLAUDE.md edge cases -------------------------------------------------


class TestClaudeMdEdges:
    def test_deploy_skips_empty_claude_md(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "CLAUDE.md").write_text("   \n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        assert not (workdir / "CLAUDE.md").exists()

    def test_claude_md_with_skills_block(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        # Add a skills config so build_skills_lines returns content
        cfg.skills = MagicMock()
        cfg.skills.required = ["my-skill"]
        cfg.skills.available = []
        cfg.skills.injection_mode = "block"
        cfg.skills.match_by = ["skill-id"]
        cfg.skills.match_style = "exact"
        (tmp_path / "agent_def" / "dot_claude" / "CLAUDE.md").write_text("body\n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        content = (workdir / "CLAUDE.md").read_text()
        assert "body" in content
