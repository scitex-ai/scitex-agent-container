"""Coverage for the .env / state.md / extras-mirror branches of
``runtimes._dot_claude`` and the small helper edge-cases not covered
by ``test__dot_claude.py``.

These tests target previously-uncovered ranges:
  - ``_deploy_env``    (lines ~352-372)
  - ``_cleanup_env``   (lines ~379-382)
  - ``_deploy_state_md``  (lines ~389-399)
  - ``_cleanup_state_md`` (lines ~405-411)
  - ``_iter_extras`` / ``_deploy_extras`` / ``_cleanup_extras``
  - ``resolve_dot_claude_dir`` absolute-path + relative-path branches
  - ``_interpolate_metadata`` label substitution
  - ``_deploy_mcp_json`` malformed-JSON tolerance branches
  - ``deploy_dot_claude`` re-run idempotency for the umbrella
"""

from __future__ import annotations

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
