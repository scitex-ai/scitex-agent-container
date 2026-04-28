"""Tests for continue-or-new session restart strategy.

Covers:
    - ``_session_resumable`` detection helper
    - ``ClaudeCodeRuntime._build_command`` mode handling
    - ``parse_claude`` default + top-level ``session:`` override
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scitex_agent_container.config import AgentConfig, ClaudeSpec
from scitex_agent_container.config._parsers import parse_claude
from scitex_agent_container.runtimes.claude_code import (
    ClaudeCodeRuntime,
    _encode_workdir_for_claude_projects,
    _session_resumable,
)


# ---------------------------------------------------------------------------
# _session_resumable
# ---------------------------------------------------------------------------


def _make_fake_home(tmp_path: Path, workdir: str) -> Path:
    """Create a fake ~/.claude/projects/<encoded>/ dir under tmp_path."""
    encoded = _encode_workdir_for_claude_projects(workdir)
    proj = tmp_path / ".claude" / "projects" / encoded
    proj.mkdir(parents=True)
    return proj


class TestEncodeWorkdir:
    def test_basic(self):
        assert (
            _encode_workdir_for_claude_projects("/Users/ywatanabe/foo")
            == "-Users-ywatanabe-foo"
        )

    def test_dot_prefix_segment_produces_double_dash(self):
        # Claude Code replaces both ``/`` and ``.`` with ``-``, so
        # ``/.dotfiles`` becomes ``--dotfiles``.
        assert (
            _encode_workdir_for_claude_projects("/Users/ywatanabe/.dotfiles")
            == "-Users-ywatanabe--dotfiles"
        )

    def test_scitex_workspace_path_matches_disk(self):
        # Regression: the lead/proj/contributor workspaces under
        # ~/.scitex/agent-container/workspaces/<name>/ must encode to the
        # exact dirname Claude Code uses on disk, otherwise --continue is
        # silently dropped on every restart.
        assert (
            _encode_workdir_for_claude_projects(
                "/home/ywatanabe/.scitex/agent-container/workspaces/lead"
            )
            == "-home-ywatanabe--scitex-agent-container-workspaces-lead"
        )

    def test_triple_or_more_dashes_collapse_to_double(self):
        # ``/..foo`` would naively expand to ``---foo`` (slash + two dots);
        # Claude Code collapses runs of 3+ dashes back to ``--``.
        assert (
            _encode_workdir_for_claude_projects("/Users/ywatanabe/..foo")
            == "-Users-ywatanabe--foo"
        )


class TestSessionResumable:
    def test_missing_projects_dir(self, tmp_path):
        assert _session_resumable("/nonexistent/workdir", user_home=str(tmp_path)) is False

    def test_empty_projects_dir(self, tmp_path):
        workdir = "/fake/workdir"
        _make_fake_home(tmp_path, workdir)
        assert _session_resumable(workdir, user_home=str(tmp_path)) is False

    def test_empty_jsonl_file(self, tmp_path):
        workdir = "/fake/workdir"
        proj = _make_fake_home(tmp_path, workdir)
        (proj / "abc.jsonl").write_text("")
        assert _session_resumable(workdir, user_home=str(tmp_path)) is False

    def test_nonempty_jsonl_file(self, tmp_path):
        workdir = "/fake/workdir"
        proj = _make_fake_home(tmp_path, workdir)
        (proj / "abc.jsonl").write_text('{"role":"user"}\n')
        assert _session_resumable(workdir, user_home=str(tmp_path)) is True

    def test_nonjsonl_files_ignored(self, tmp_path):
        workdir = "/fake/workdir"
        proj = _make_fake_home(tmp_path, workdir)
        (proj / "notes.txt").write_text("lots of content")
        assert _session_resumable(workdir, user_home=str(tmp_path)) is False


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def _cfg(self, session: str, workdir: str = "/fake/workdir") -> AgentConfig:
        return AgentConfig(
            name="test-agent",
            workdir=workdir,
            claude=ClaudeSpec(session=session),
        )

    def test_continue_or_new_emits_continue_when_resumable(self):
        cfg = self._cfg("continue-or-new")
        with patch(
            "scitex_agent_container.runtimes.claude_code._session_resumable",
            return_value=True,
        ):
            cmd = ClaudeCodeRuntime()._build_command(cfg)
        assert "--continue" in cmd

    def test_continue_or_new_omits_continue_when_not_resumable(self):
        cfg = self._cfg("continue-or-new")
        with patch(
            "scitex_agent_container.runtimes.claude_code._session_resumable",
            return_value=False,
        ):
            cmd = ClaudeCodeRuntime()._build_command(cfg)
        assert "--continue" not in cmd

    def test_continue_mode_always_emits_continue(self):
        cfg = self._cfg("continue")
        with patch(
            "scitex_agent_container.runtimes.claude_code._session_resumable",
            return_value=False,
        ):
            cmd = ClaudeCodeRuntime()._build_command(cfg)
        assert "--continue" in cmd

    def test_new_mode_never_emits_continue(self):
        cfg = self._cfg("new")
        with patch(
            "scitex_agent_container.runtimes.claude_code._session_resumable",
            return_value=True,
        ):
            cmd = ClaudeCodeRuntime()._build_command(cfg)
        assert "--continue" not in cmd


# ---------------------------------------------------------------------------
# parse_claude
# ---------------------------------------------------------------------------


class TestParseClaude:
    def test_default_is_continue_or_new(self):
        spec = parse_claude({})
        assert spec.session == "continue-or-new"

    def test_nested_claude_session_respected(self):
        spec = parse_claude({"claude": {"session": "new"}})
        assert spec.session == "new"

    def test_top_level_session_overrides_nested(self):
        spec = parse_claude({"session": "continue", "claude": {"session": "new"}})
        assert spec.session == "continue"

    def test_top_level_session_alone(self):
        spec = parse_claude({"session": "new"})
        assert spec.session == "new"
