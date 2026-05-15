"""Tests for the dot_claude/ deploy + cleanup pipeline.

Replaces test_src_files.py — the legacy ``src_<X>`` sibling-file
convention was retired in favour of a single ``dot_claude/`` directory
next to ``spec.yaml`` (F-DC1). Same invariants are preserved:

  - CLAUDE.md  — marker-protected, user-tail-preserving overwrite
  - .mcp.json  — per-server replace, other servers preserved
  - .env       — full overwrite, mode 0600
  - state.md   — full overwrite

PA-306 no-mocks: every test exercises real ``AgentConfig`` instances
(real ``@dataclass`` from :mod:`scitex_agent_container.config._types`)
against the real filesystem under ``tmp_path``. Env-driven tests use
the ``env_save_restore`` fixture (POSIX-honest equivalent of
``monkeypatch.setenv`` / ``monkeypatch.delenv``).

One legacy test was deleted: ``test_chmod_failure_is_warned_not_raised``
— relied on ``monkeypatch.setattr(dc.os, "chmod", _boom)``. There is no
portable, honest filesystem seam to exercise the warn-but-don't-raise
branch (no real test-runner user is unable to ``chmod`` a file they
just wrote). Honest-delete is preferred over a dishonest rewrite.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig, SkillsSpec
from scitex_agent_container.runtimes._dot_claude import (
    END_MARKER,
    _extract_user_tail,
    _interpolate_env,
    _interpolate_metadata,
    _iter_extras,
    cleanup_dot_claude,
    deploy_dot_claude,
    resolve_dot_claude_dir,
)

START_MARKER_RE = re.compile(
    r"<!-- Start of scitex-agent-container generated section.*?-->"
)

SRC_BODY = "## Agent: test-agent\n\n### Role\nTest agent.\n"


# ---------------------------------------------------------------------------
# Real-AgentConfig builders (replace _make_config / _make_mcp_config / _make_cfg
# legacy MagicMock factories — honest seams only).
# ---------------------------------------------------------------------------


def _build_cfg(
    tmp_path: Path,
    *,
    claude_md_content: str | None = None,
    dot_claude: str = "",
    labels: dict | None = None,
) -> AgentConfig:
    """Build a real ``AgentConfig`` pointing at a ``dot_claude/`` next to
    ``spec.yaml`` inside ``tmp_path``. Optionally seeds ``CLAUDE.md``.
    """
    agent_dir = tmp_path / "agent_def"
    dot_dir = agent_dir / "dot_claude"
    dot_dir.mkdir(parents=True, exist_ok=True)
    if claude_md_content is not None:
        (dot_dir / "CLAUDE.md").write_text(claude_md_content)
    cfg = AgentConfig(name="test-agent")
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.dot_claude = dot_claude
    if labels:
        cfg.labels = dict(labels)
    return cfg


def _build_cfg_with_mcp(tmp_path: Path, servers: dict) -> AgentConfig:
    """Build a real ``AgentConfig`` with ``dot_claude/.mcp.json`` pre-written."""
    cfg = _build_cfg(tmp_path)
    root = tmp_path / "agent_def" / "dot_claude"
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return cfg


# ---------------------------------------------------------------------------
# _extract_user_tail
# ---------------------------------------------------------------------------


class TestExtractUserTail:
    def test_returns_empty_when_file_missing(self, tmp_path):
        # Arrange
        missing = tmp_path / "nonexistent.md"
        # Act
        result = _extract_user_tail(missing)
        # Assert
        assert result == ""

    def test_returns_empty_when_marker_absent(self, tmp_path):
        # Arrange
        f = tmp_path / "CLAUDE.md"
        f.write_text("no markers here\n")
        # Act
        result = _extract_user_tail(f)
        # Assert
        assert result == ""

    def test_returns_tail_after_marker(self, tmp_path):
        # Arrange
        f = tmp_path / "CLAUDE.md"
        f.write_text(f"generated\n{END_MARKER}\nmy notes\n")
        # Act
        result = _extract_user_tail(f)
        # Assert
        assert result == "\nmy notes\n"

    def test_uses_last_marker_occurrence(self, tmp_path):
        # Arrange
        f = tmp_path / "CLAUDE.md"
        f.write_text(f"{END_MARKER}\nold\n{END_MARKER}\nnew tail\n")
        # Act
        result = _extract_user_tail(f)
        # Assert
        assert result == "\nnew tail\n"


# ---------------------------------------------------------------------------
# CLAUDE.md deploy
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_claude_md(tmp_path: Path) -> tuple[AgentConfig, Path, str]:
    """Run ``deploy_dot_claude`` once with a seed ``CLAUDE.md`` and return
    the cfg + dest path + content (one shared setup → many single-assert tests).
    """
    cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
    workdir = str(tmp_path / "workspace")
    deploy_dot_claude(cfg, workdir)
    dest = Path(workdir) / "CLAUDE.md"
    return cfg, dest, dest.read_text()


class TestDeployClaudeMd:
    def test_fresh_deploy_creates_destination_file(self, fresh_claude_md):
        # Arrange
        _cfg, dest, _content = fresh_claude_md
        # Act
        exists = dest.exists()
        # Assert
        assert exists

    def test_fresh_deploy_emits_start_marker(self, fresh_claude_md):
        # Arrange
        _cfg, _dest, content = fresh_claude_md
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_fresh_deploy_emits_end_marker(self, fresh_claude_md):
        # Arrange
        _cfg, _dest, content = fresh_claude_md
        # Act
        # (Assertion exercises the End marker presence.)
        # Assert
        assert END_MARKER in content

    def test_fresh_deploy_includes_agent_name_in_body(self, fresh_claude_md):
        # Arrange
        _cfg, _dest, content = fresh_claude_md
        # Act
        # Assert
        assert "test-agent" in content

    def test_guide_comment_mentions_custom_content(self, fresh_claude_md):
        # Arrange
        _cfg, _dest, content = fresh_claude_md
        end_idx = content.index(END_MARKER)
        tail = content[end_idx + len(END_MARKER) :].lower()
        # Act
        # Assert
        assert "custom content" in tail

    def test_guide_comment_mentions_overwritten(self, fresh_claude_md):
        # Arrange
        _cfg, _dest, content = fresh_claude_md
        end_idx = content.index(END_MARKER)
        tail = content[end_idx + len(END_MARKER) :].lower()
        # Act
        # Assert
        assert "overwritten" in tail

    def test_user_tail_preserved_across_redeploy(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / "CLAUDE.md"
        dest.write_text(dest.read_text() + "\n### My Notes\nremember this\n")
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        assert "remember this" in dest.read_text()

    def test_guide_comment_not_duplicated_on_redeploy(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        content = (Path(workdir) / "CLAUDE.md").read_text()
        assert content.count("CUSTOM CONTENT") <= 1

    def test_multiple_start_markers_raises_runtime_error(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        bad = (
            "<!-- Start of scitex-agent-container generated section (ts1) -->\n"
            f"{END_MARKER}\n"
            "<!-- Start of scitex-agent-container generated section (ts2) -->\n"
            f"{END_MARKER}\n"
        )
        (workdir / "CLAUDE.md").write_text(bad)
        # Act
        # Assert
        with pytest.raises(RuntimeError, match="expected exactly 1"):
            deploy_dot_claude(cfg, str(workdir))

    def test_missing_dot_claude_dir_is_noop(self, tmp_path):
        # Arrange — real AgentConfig pointing at a non-existent spec dir
        # so resolve_dot_claude_dir returns None.
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.dot_claude = ""
        workdir = str(tmp_path / "ws")
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        assert not (Path(workdir) / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# Cleanup → deploy round-trip (regression: stop→start race)
# ---------------------------------------------------------------------------


@pytest.fixture
def redeploy_after_cleanup(tmp_path: Path) -> tuple[Path, str]:
    """Deploy → cleanup → deploy. Return dest path + content."""
    cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
    workdir = str(tmp_path / "workspace")
    deploy_dot_claude(cfg, workdir)
    cleanup_dot_claude(cfg, workdir)
    deploy_dot_claude(cfg, workdir)
    dest = Path(workdir) / "CLAUDE.md"
    return dest, dest.read_text()


@pytest.fixture
def cleanup_with_user_tail(tmp_path: Path) -> tuple[Path, str]:
    """Deploy → user appends notes → cleanup. Return dest + content."""
    cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
    workdir = str(tmp_path / "workspace")
    deploy_dot_claude(cfg, workdir)
    dest = Path(workdir) / "CLAUDE.md"
    dest.write_text(dest.read_text() + "\n### My Notes\nremember this\n")
    cleanup_dot_claude(cfg, workdir)
    return dest, dest.read_text()


class TestCleanupStopStartRoundTrip:
    """Regression — the stop→start race that bricked 9 mamba agents on
    MBA on 2026-04-15: cleanup must strip the managed block AND its
    guide comment, otherwise the next deploy's marker validator rejects
    the orphan block."""

    def test_redeploy_after_cleanup_creates_file(self, redeploy_after_cleanup):
        # Arrange
        dest, _content = redeploy_after_cleanup
        # Act
        # Assert
        assert dest.exists()

    def test_redeploy_after_cleanup_emits_start_marker(self, redeploy_after_cleanup):
        # Arrange
        _dest, content = redeploy_after_cleanup
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_redeploy_after_cleanup_emits_end_marker(self, redeploy_after_cleanup):
        # Arrange
        _dest, content = redeploy_after_cleanup
        # Act
        # Assert
        assert END_MARKER in content

    def test_cleanup_removes_file_when_only_managed_section(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
        workdir = str(tmp_path / "workspace")
        deploy_dot_claude(cfg, workdir)
        # Act
        cleanup_dot_claude(cfg, workdir)
        # Assert
        assert not (Path(workdir) / "CLAUDE.md").exists()

    def test_cleanup_keeps_file_when_user_tail_present(self, cleanup_with_user_tail):
        # Arrange
        dest, _remaining = cleanup_with_user_tail
        # Act
        # Assert
        assert dest.exists()

    def test_cleanup_preserves_user_tail_content(self, cleanup_with_user_tail):
        # Arrange
        _dest, remaining = cleanup_with_user_tail
        # Act
        # Assert
        assert "remember this" in remaining

    def test_cleanup_strips_start_marker_line(self, cleanup_with_user_tail):
        # Arrange
        _dest, remaining = cleanup_with_user_tail
        # Act
        # Assert
        assert "Start of scitex-agent-container" not in remaining

    def test_cleanup_strips_guide_comment_marker(self, cleanup_with_user_tail):
        # Arrange
        _dest, remaining = cleanup_with_user_tail
        # Act
        # Assert
        assert "CUSTOM CONTENT" not in remaining

    def test_cleanup_strips_legacy_guide_comment(self, tmp_path):
        # Arrange
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        legacy = (
            "<!-- Start of scitex-agent-container generated section (old) -->\n"
            "body\n"
            f"{END_MARKER}\n"
            "<!-- ↓ Your custom content goes here -->\n"
        )
        (workdir / "CLAUDE.md").write_text(legacy)
        cfg = _build_cfg(tmp_path, claude_md_content=SRC_BODY)
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# .mcp.json deploy
# ---------------------------------------------------------------------------


def _server_entry(channels: str) -> dict:
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


@pytest.fixture
def fresh_mcp_deploy(tmp_path: Path) -> Path:
    """Run a fresh ``deploy_dot_claude`` with one scitex-orochi server."""
    workdir = str(tmp_path / "workspace")
    cfg = _build_cfg_with_mcp(tmp_path, _server_entry("#ywatanabe,#heads"))
    deploy_dot_claude(cfg, workdir)
    return Path(workdir) / ".mcp.json"


class TestDeployMcpJsonRefresh:
    """Regression — todo#453: workspace .mcp.json must refresh from the
    canonical source on EVERY deploy, not just first launch."""

    def test_fresh_deploy_writes_workspace_copy_file(self, fresh_mcp_deploy):
        # Arrange
        dest = fresh_mcp_deploy
        # Act
        # Assert
        assert dest.exists()

    def test_fresh_deploy_writes_channels_env(self, fresh_mcp_deploy):
        # Arrange
        dest = fresh_mcp_deploy
        data = json.loads(dest.read_text())
        # Act
        channels = data["mcpServers"]["scitex-orochi"]["env"]["SCITEX_OROCHI_CHANNELS"]
        # Assert
        assert channels == "#ywatanabe,#heads"

    def test_env_change_propagates_on_redeploy(self, tmp_path):
        # Arrange
        workdir = str(tmp_path / "workspace")
        cfg = _build_cfg_with_mcp(tmp_path, _server_entry("#ywatanabe,#heads"))
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps({"mcpServers": _server_entry("#ywatanabe,#heads,#lead,#agent")})
        )
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        channels = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"][
            "SCITEX_OROCHI_CHANNELS"
        ]
        assert channels == "#ywatanabe,#heads,#lead,#agent"

    def test_removed_env_key_is_dropped_on_redeploy(self, tmp_path):
        # Arrange
        workdir = str(tmp_path / "workspace")
        cfg = _build_cfg_with_mcp(
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
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        assert "LEGACY_KEY" not in env

    def test_redeploy_interpolates_metadata_name(self, tmp_path):
        # Arrange
        workdir = str(tmp_path / "workspace")
        cfg = _build_cfg_with_mcp(
            tmp_path,
            {
                "scitex-orochi": {
                    "command": "bun",
                    "env": {"SCITEX_OROCHI_AGENT": "${metadata.name}"},
                }
            },
        )
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        env = json.loads((Path(workdir) / ".mcp.json").read_text())["mcpServers"][
            "scitex-orochi"
        ]["env"]
        assert env["SCITEX_OROCHI_AGENT"] == "test-agent"

    @pytest.fixture
    def merged_servers_workspace(self, tmp_path: Path) -> dict:
        workdir = Path(tmp_path / "workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = _build_cfg_with_mcp(tmp_path, _server_entry("#a,#b"))
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
        return json.loads((workdir / ".mcp.json").read_text())

    def test_other_servers_preserved_local_tool(self, merged_servers_workspace):
        # Arrange
        data = merged_servers_workspace
        # Act
        # Assert
        assert "local-tool" in data["mcpServers"]

    def test_other_servers_preserved_scitex_orochi(self, merged_servers_workspace):
        # Arrange
        data = merged_servers_workspace
        # Act
        # Assert
        assert "scitex-orochi" in data["mcpServers"]

    def test_unconditional_refresh_even_when_src_older(self, tmp_path):
        # Arrange
        workdir = str(tmp_path / "workspace")
        cfg = _build_cfg_with_mcp(tmp_path, _server_entry("#v2"))
        deploy_dot_claude(cfg, workdir)
        dest = Path(workdir) / ".mcp.json"
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": _server_entry("#v3")}))
        ancient = dest.stat().st_mtime - 3600
        os.utime(src, (ancient, ancient))
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        env = json.loads(dest.read_text())["mcpServers"]["scitex-orochi"]["env"]
        assert env["SCITEX_OROCHI_CHANNELS"] == "#v3"

    def test_idempotent_when_src_unchanged(self, tmp_path):
        # Arrange
        workdir = str(tmp_path / "workspace")
        cfg = _build_cfg_with_mcp(tmp_path, _server_entry("#stable"))
        deploy_dot_claude(cfg, workdir)
        first = (Path(workdir) / ".mcp.json").read_text()
        # Act
        deploy_dot_claude(cfg, workdir)
        # Assert
        second = (Path(workdir) / ".mcp.json").read_text()
        assert first == second


# ---------------------------------------------------------------------------
# resolve_dot_claude_dir branches
# ---------------------------------------------------------------------------


class TestResolveDotClaudeDir:
    def test_returns_none_when_config_path_missing(self):
        # Arrange
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = ""
        cfg.dot_claude = ""
        # Act
        result = resolve_dot_claude_dir(cfg)
        # Assert
        assert result is None

    def test_relative_path_resolves_against_spec_dir(self, tmp_path):
        # Arrange
        custom = tmp_path / "agent_def" / "my_dot"
        custom.mkdir(parents=True)
        cfg = _build_cfg(tmp_path, dot_claude="my_dot")
        # Act
        result = resolve_dot_claude_dir(cfg)
        # Assert
        assert result == custom

    def test_absolute_path_used_directly(self, tmp_path):
        # Arrange
        custom = tmp_path / "abs_dot"
        custom.mkdir()
        cfg = _build_cfg(tmp_path, dot_claude=str(custom))
        # Act
        result = resolve_dot_claude_dir(cfg)
        # Assert
        assert result == custom

    def test_relative_path_without_spec_dir_returns_none(self):
        # Arrange
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = ""
        cfg.dot_claude = "relative_dir"
        # Act
        result = resolve_dot_claude_dir(cfg)
        # Assert
        assert result is None

    def test_nonexistent_path_returns_none(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path, dot_claude=str(tmp_path / "does_not_exist"))
        # Act
        result = resolve_dot_claude_dir(cfg)
        # Assert
        assert result is None


# ---------------------------------------------------------------------------
# Interpolation helpers
# ---------------------------------------------------------------------------


class TestInterpolation:
    def test_metadata_name_substitution(self):
        # Arrange
        cfg = AgentConfig(name="myagent")
        # Act
        out = _interpolate_metadata("hello ${metadata.name}", cfg)
        # Assert
        assert out == "hello myagent"

    def test_metadata_labels_substitution(self):
        # Arrange
        cfg = AgentConfig(name="x")
        cfg.labels = {"role": "head"}
        # Act
        out = _interpolate_metadata("R=${metadata.labels.role}", cfg)
        # Assert
        assert out == "R=head"

    def test_metadata_unknown_label_left_in_place(self):
        # Arrange
        cfg = AgentConfig(name="x")
        original = "X=${metadata.labels.missing}"
        # Act
        out = _interpolate_metadata(original, cfg)
        # Assert
        assert out == original

    def test_metadata_unknown_key_left_in_place(self):
        # Arrange
        cfg = AgentConfig(name="x")
        original = "X=${something.else}"
        # Act
        out = _interpolate_metadata(original, cfg)
        # Assert
        assert out == original

    def test_env_variable_substituted_when_set(self, env_save_restore):
        # Arrange
        env_save_restore.set("MY_TEST_VAR", "VALUE")
        # Act
        out = _interpolate_env("X=${MY_TEST_VAR}")
        # Assert
        assert out == "X=VALUE"

    def test_env_missing_left_in_place(self, env_save_restore):
        # Arrange
        env_save_restore.delete("NEVER_SET_VAR_XYZ")
        # Act
        out = _interpolate_env("X=${NEVER_SET_VAR_XYZ}")
        # Assert
        assert out == "X=${NEVER_SET_VAR_XYZ}"


# ---------------------------------------------------------------------------
# .env deploy / cleanup
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed_env(tmp_path: Path) -> Path:
    """Deploy a .env containing one interpolation and one literal."""
    cfg = _build_cfg(tmp_path)
    (tmp_path / "agent_def" / "dot_claude" / ".env").write_text(
        "API_KEY=abc\nNAME=${metadata.name}\n"
    )
    workdir = tmp_path / "ws"
    deploy_dot_claude(cfg, str(workdir))
    return workdir / ".env"


class TestDeployEnv:
    def test_deploy_env_creates_file(self, deployed_env):
        # Arrange
        dest = deployed_env
        # Act
        # Assert
        assert dest.exists()

    def test_deploy_env_writes_literal_value(self, deployed_env):
        # Arrange
        dest = deployed_env
        # Act
        text = dest.read_text()
        # Assert
        assert "API_KEY=abc" in text

    def test_deploy_env_interpolates_metadata_name(self, deployed_env):
        # Arrange
        dest = deployed_env
        # Act
        text = dest.read_text()
        # Assert
        assert "NAME=test-agent" in text

    def test_deploy_env_writes_mode_0600(self, deployed_env):
        # Arrange
        dest = deployed_env
        # Act
        mode = stat.S_IMODE(dest.stat().st_mode)
        # Assert
        assert mode == 0o600

    def test_empty_env_is_skipped(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("   \n")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / ".env").exists()

    def test_appends_trailing_newline(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("X=1")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        assert (workdir / ".env").read_text().endswith("\n")

    def test_cleanup_env_removes_workspace_copy(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".env").write_text("X=1\n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / ".env").exists()

    def test_cleanup_env_noop_when_src_absent(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".env").write_text("X=1\n")
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — no src .env → cleanup does NOT remove workspace .env
        assert (workdir / ".env").exists()


# NOTE: ``test_chmod_failure_is_warned_not_raised`` (legacy) was DELETED.
# It relied on ``monkeypatch.setattr(dc.os, "chmod", _boom)`` — a forbidden
# mock seam. There is no portable filesystem-level way to make ``os.chmod``
# fail on a freshly-written user-owned file under a typical Linux test
# runner. The warn-but-don't-raise branch remains in production for genuine
# OS-level failures (read-only FS / SELinux); we accept the coverage gap
# rather than fake the failure.


# ---------------------------------------------------------------------------
# state.md deploy / cleanup
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed_state_md(tmp_path: Path) -> Path:
    cfg = _build_cfg(tmp_path)
    (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text(
        "# State for ${metadata.name}"
    )
    workdir = tmp_path / "ws"
    deploy_dot_claude(cfg, str(workdir))
    return workdir / "state.md"


class TestDeployStateMd:
    def test_deploy_state_interpolates_metadata_name(self, deployed_state_md):
        # Arrange
        dest = deployed_state_md
        # Act
        text = dest.read_text()
        # Assert
        assert "test-agent" in text

    def test_deploy_state_appends_trailing_newline(self, deployed_state_md):
        # Arrange
        dest = deployed_state_md
        # Act
        text = dest.read_text()
        # Assert
        assert text.endswith("\n")

    def test_empty_state_skipped(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text("   \n")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / "state.md").exists()

    def test_cleanup_removes_state_md(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "state.md").write_text("# s\n")
        workdir = tmp_path / "ws"
        deploy_dot_claude(cfg, str(workdir))
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / "state.md").exists()

    def test_cleanup_noop_when_state_md_missing(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        workdir = tmp_path / "ws"
        workdir.mkdir()
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — should not raise; file remains absent.
        assert not (workdir / "state.md").exists()


# ---------------------------------------------------------------------------
# Extras (mirror everything else under .claude/)
# ---------------------------------------------------------------------------


@pytest.fixture
def extras_dot_root(tmp_path: Path) -> Path:
    root = tmp_path / "dot"
    root.mkdir()
    (root / ".DS_Store").write_text("")
    (root / "hooks").mkdir()
    return root


@pytest.fixture
def deployed_extras_claude(tmp_path: Path) -> Path:
    cfg = _build_cfg(tmp_path)
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
    return workdir / ".claude"


@pytest.fixture
def overwritten_extras_cmds(tmp_path: Path) -> Path:
    cfg = _build_cfg(tmp_path)
    root = tmp_path / "agent_def" / "dot_claude"
    (root / "commands").mkdir()
    (root / "commands" / "new.md").write_text("new")
    workdir = tmp_path / "ws"
    stale = workdir / ".claude" / "commands"
    stale.mkdir(parents=True)
    (stale / "stale.md").write_text("stale")
    deploy_dot_claude(cfg, str(workdir))
    return workdir / ".claude" / "commands"


@pytest.fixture
def cleaned_extras_workdir(tmp_path: Path) -> Path:
    cfg = _build_cfg(tmp_path)
    root = tmp_path / "agent_def" / "dot_claude"
    (root / "commands").mkdir()
    (root / "commands" / "x.md").write_text("x")
    (root / "loose.md").write_text("loose")
    workdir = tmp_path / "ws"
    deploy_dot_claude(cfg, str(workdir))
    cleanup_dot_claude(cfg, str(workdir))
    return workdir


class TestExtras:
    def test_iter_extras_skips_known_root_files(self, tmp_path):
        # Arrange
        root = tmp_path / "dot"
        root.mkdir()
        (root / "CLAUDE.md").write_text("x")
        (root / ".env").write_text("x")
        (root / ".mcp.json").write_text("x")
        (root / "state.md").write_text("x")
        (root / "commands").mkdir()
        (root / "skills").mkdir()
        # Act
        names = sorted(c.name for c in _iter_extras(root))
        # Assert
        assert names == ["commands", "skills"]

    def test_iter_extras_skips_ds_store_file(self, extras_dot_root):
        # Arrange
        names = [c.name for c in _iter_extras(extras_dot_root)]
        # Act
        # Assert
        assert ".DS_Store" not in names

    def test_iter_extras_includes_hooks_dir(self, extras_dot_root):
        # Arrange
        names = [c.name for c in _iter_extras(extras_dot_root)]
        # Act
        # Assert
        assert "hooks" in names

    def test_iter_extras_noop_when_root_missing(self, tmp_path):
        # Arrange
        missing = tmp_path / "nope"
        # Act
        result = list(_iter_extras(missing))
        # Assert
        assert result == []

    def test_deploy_mirrors_commands_dir(self, deployed_extras_claude):
        # Arrange
        claude = deployed_extras_claude
        # Act
        # Assert
        assert (claude / "commands" / "hi.md").read_text() == "hi"

    def test_deploy_mirrors_skills_dir(self, deployed_extras_claude):
        # Arrange
        claude = deployed_extras_claude
        # Act
        # Assert
        assert (claude / "skills" / "do.md").read_text() == "do"

    def test_deploy_mirrors_hooks_dir(self, deployed_extras_claude):
        # Arrange
        claude = deployed_extras_claude
        # Act
        # Assert
        assert (claude / "hooks" / "h.sh").read_text() == "#!/bin/sh\n"

    def test_deploy_mirrors_loose_md_at_claude_root(self, deployed_extras_claude):
        # Arrange
        claude = deployed_extras_claude
        # Act
        # Assert
        assert (claude / "loose.md").read_text() == "loose"

    def test_deploy_extras_writes_new_file(self, overwritten_extras_cmds):
        # Arrange
        cmds = overwritten_extras_cmds
        # Act
        # Assert
        assert (cmds / "new.md").exists()

    def test_deploy_extras_removes_stale_file(self, overwritten_extras_cmds):
        # Arrange
        cmds = overwritten_extras_cmds
        # Act
        # Assert
        assert not (cmds / "stale.md").exists()

    def test_cleanup_extras_removes_commands_dir(self, cleaned_extras_workdir):
        # Arrange
        workdir = cleaned_extras_workdir
        # Act
        # Assert
        assert not (workdir / ".claude" / "commands").exists()

    def test_cleanup_extras_removes_loose_file(self, cleaned_extras_workdir):
        # Arrange
        workdir = cleaned_extras_workdir
        # Act
        # Assert
        assert not (workdir / ".claude" / "loose.md").exists()

    def test_cleanup_extras_noop_when_claude_dir_missing(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "commands").mkdir()
        workdir = tmp_path / "ws"
        workdir.mkdir()
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — no .claude/ in workdir → should not raise; nothing created.
        assert not (workdir / ".claude").exists()


# ---------------------------------------------------------------------------
# Malformed-JSON tolerance in .mcp.json
# ---------------------------------------------------------------------------


@pytest.fixture
def malformed_src_mcp_deploy(tmp_path: Path, caplog):
    cfg = _build_cfg(tmp_path)
    (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text("{not json")
    workdir = tmp_path / "ws"
    # caplog.at_level inside the fixture is scoped to the fixture body in
    # pytest 8.x — capture the record messages eagerly so the test can
    # assert on them after the fixture returns.
    with caplog.at_level(logging.WARNING):
        deploy_dot_claude(cfg, str(workdir))
        messages = [r.getMessage() for r in caplog.records]
    return workdir, messages


@pytest.fixture
def tilde_args_deployed(tmp_path: Path) -> list[str]:
    cfg = _build_cfg(tmp_path)
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
    return json.loads((workdir / ".mcp.json").read_text())["mcpServers"]["tool"]["args"]


class TestMcpJsonRobustness:
    def test_src_malformed_json_skips_dest_write(self, malformed_src_mcp_deploy):
        # Arrange
        workdir, _messages = malformed_src_mcp_deploy
        # Act
        # Assert
        assert not (workdir / ".mcp.json").exists()

    def test_src_malformed_json_emits_warning(self, malformed_src_mcp_deploy):
        # Arrange
        _workdir, messages = malformed_src_mcp_deploy
        # Act
        # Assert
        assert any("Invalid JSON" in m for m in messages)

    def test_src_empty_mcp_json_is_skipped(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text("  \n")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / ".mcp.json").exists()

    def test_existing_workspace_malformed_json_treated_as_empty(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}})
        )
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text("broken{")
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "a" in data["mcpServers"]

    def test_existing_workspace_list_treated_as_empty(self, tmp_path):
        """If dest .mcp.json is a JSON array (not dict), drop it."""
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}})
        )
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(json.dumps(["array", "form"]))
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "a" in data["mcpServers"]

    def test_tilde_args_first_element_unchanged(self, tilde_args_deployed):
        # Arrange
        args = tilde_args_deployed
        # Act
        # Assert
        assert args[0] == "run"

    def test_tilde_args_second_element_expanded(self, tilde_args_deployed):
        # Arrange
        args = tilde_args_deployed
        # Act
        # Assert
        assert not args[1].startswith("~")

    def test_tilde_args_second_element_keeps_basename(self, tilde_args_deployed):
        # Arrange
        args = tilde_args_deployed
        # Act
        # Assert
        assert args[1].endswith("script.ts")

    def test_tilde_args_third_element_unchanged(self, tilde_args_deployed):
        # Arrange
        args = tilde_args_deployed
        # Act
        # Assert
        assert args[2] == "--flag"


# ---------------------------------------------------------------------------
# cleanup_mcp_json edge cases
# ---------------------------------------------------------------------------


@pytest.fixture
def cleaned_mcp_with_user_server(tmp_path: Path) -> dict:
    cfg = _build_cfg(tmp_path)
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
    return json.loads((workdir / ".mcp.json").read_text())


class TestCleanupMcpJson:
    def test_cleanup_removes_managed_server(self, cleaned_mcp_with_user_server):
        # Arrange
        data = cleaned_mcp_with_user_server
        # Act
        # Assert
        assert "mng" not in data["mcpServers"]

    def test_cleanup_keeps_user_local_server(self, cleaned_mcp_with_user_server):
        # Arrange
        data = cleaned_mcp_with_user_server
        # Act
        # Assert
        assert "user-local" in data["mcpServers"]

    def test_cleanup_removes_file_when_empty_after(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"only": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"only": {"command": "x"}}})
        )
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / ".mcp.json").exists()

    def test_cleanup_noop_when_src_malformed(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text("not json")
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"keep": {"command": "x"}}})
        )
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "keep" in data["mcpServers"]

    def test_cleanup_noop_when_src_has_no_servers(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"keep": {"command": "x"}}})
        )
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — no servers to remove → workspace unchanged.
        data = json.loads((workdir / ".mcp.json").read_text())
        assert "keep" in data["mcpServers"]

    def test_cleanup_noop_when_dest_missing(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — must not raise; nothing created.
        assert not (workdir / ".mcp.json").exists()

    def test_cleanup_noop_when_dest_malformed(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        src = tmp_path / "agent_def" / "dot_claude" / ".mcp.json"
        src.write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        workdir = tmp_path / "ws"
        workdir.mkdir()
        (workdir / ".mcp.json").write_text("not json {")
        # Act
        cleanup_dot_claude(cfg, str(workdir))
        # Assert — should not raise; malformed file is left as-is.
        assert (workdir / ".mcp.json").exists()


# ---------------------------------------------------------------------------
# CLAUDE.md edge cases
# ---------------------------------------------------------------------------


class TestClaudeMdEdges:
    def test_deploy_skips_empty_claude_md(self, tmp_path):
        # Arrange
        cfg = _build_cfg(tmp_path)
        (tmp_path / "agent_def" / "dot_claude" / "CLAUDE.md").write_text("   \n")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        assert not (workdir / "CLAUDE.md").exists()

    def test_claude_md_with_skills_block_includes_body(self, tmp_path):
        # Arrange — real SkillsSpec with one required skill, "block" mode
        # so build_skills_lines emits a fenced block (no path resolution
        # required for that branch).
        cfg = _build_cfg(tmp_path)
        cfg.skills = SkillsSpec(
            required=["my-skill"],
            available=[],
            injection_mode="block",
            match_by=["skill-id"],
            match_style="exact",
        )
        (tmp_path / "agent_def" / "dot_claude" / "CLAUDE.md").write_text("body\n")
        workdir = tmp_path / "ws"
        # Act
        deploy_dot_claude(cfg, str(workdir))
        # Assert
        content = (workdir / "CLAUDE.md").read_text()
        assert "body" in content
