"""Tests for ``sac agent send``.

Covers the resume-path wrapper from SAC_OROCHI_SCOPES.md §6 step 1:
read session_id → cd workdir → exec claude --resume <sid> -p.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.send_cmds import send


def _seed_agent(tmp_path: Path, name: str, session_id: str) -> Path:
    """Build a minimal v3 spec + state_dir with a recorded session_id.

    Returns the SCITEX_AGENT_CONTAINER_YAML_DIRS-style search root so
    ``resolve_config(name)`` walks here.
    """
    yaml_root = tmp_path / "agents"
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(
        f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  workdir: {tmp_path / "workdir"}
"""
    )
    (tmp_path / "workdir").mkdir()

    state_dir = tmp_path / "state" / name
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text(session_id, encoding="utf-8")

    return yaml_root


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point resolver + state-dir at tmp_path."""
    yaml_root = _seed_agent(tmp_path, "alpha", "abc-123-def")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_root))
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.send_cmds.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    return tmp_path


def test_rejects_neither_prompt_nor_key(isolated_env):
    runner = CliRunner()
    result = runner.invoke(send, ["alpha"])
    assert result.exit_code != 0
    assert "Either PROMPT or --key is required" in result.output


def test_rejects_both_prompt_and_key(isolated_env):
    runner = CliRunner()
    result = runner.invoke(send, ["alpha", "hello", "--key", "ESC"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_key_esc_sends_sigint(isolated_env, monkeypatch):
    (isolated_env / "state" / "alpha" / "pid").write_text("4242")
    killed = {}
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.send_cmds.os.kill",
        lambda pid, sig: killed.update(pid=pid, sig=sig),
    )
    runner = CliRunner()
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    assert result.exit_code == 0, result.output
    assert killed["pid"] == 4242
    assert killed["sig"] == 2  # SIGINT


def test_key_unsupported_is_usage_error(isolated_env):
    runner = CliRunner()
    result = runner.invoke(send, ["alpha", "--key", "F12"])
    assert result.exit_code != 0
    assert "not supported" in result.output


def test_key_missing_pid_errors_clearly(isolated_env):
    runner = CliRunner()
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    assert result.exit_code != 0
    assert "not running" in result.output


def test_missing_session_id_errors_clearly(tmp_path, monkeypatch):
    yaml_root = _seed_agent(tmp_path, "alpha", "sid-1")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_root))
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.send_cmds.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    # remove the session_id file
    (tmp_path / "state" / "alpha" / "session_id").unlink()

    runner = CliRunner()
    with patch(
        "scitex_agent_container.cli_pkg.send_cmds._find_claude_binary",
        return_value="/usr/bin/true",
    ):
        result = runner.invoke(send, ["alpha", "hi"])
    assert result.exit_code != 0
    assert "No session_id recorded" in result.output


def test_happy_path_invokes_claude_with_resume_in_workdir(isolated_env):
    """End-to-end: resolves spec, reads session_id, calls claude with the
    right argv in the agent's workdir."""
    runner = CliRunner()
    captured: dict = {}

    def fake_call(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return 0

    with (
        patch(
            "scitex_agent_container.cli_pkg.send_cmds._find_claude_binary",
            return_value="/usr/local/bin/claude",
        ),
        patch(
            "scitex_agent_container.cli_pkg.send_cmds.subprocess.call",
            side_effect=fake_call,
        ),
    ):
        result = runner.invoke(send, ["alpha", "follow up please"])

    assert result.exit_code == 0, result.output
    assert captured["argv"][:5] == [
        "/usr/local/bin/claude",
        "--resume",
        "abc-123-def",
        "-p",
        "follow up please",
    ]
    assert "--output-format" in captured["argv"]
    assert "stream-json" in captured["argv"]
    assert str(isolated_env / "workdir") == captured["cwd"]


def test_no_stream_strips_stream_args(isolated_env):
    runner = CliRunner()
    captured: dict = {}
    with (
        patch(
            "scitex_agent_container.cli_pkg.send_cmds._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container.cli_pkg.send_cmds.subprocess.call",
            side_effect=lambda argv, cwd=None: captured.update(argv=argv) or 0,
        ),
    ):
        result = runner.invoke(send, ["alpha", "hello", "--no-stream"])
    assert result.exit_code == 0
    assert "stream-json" not in captured["argv"]
    assert "--output-format" not in captured["argv"]


def test_model_and_max_turns_forwarded(isolated_env):
    runner = CliRunner()
    captured: dict = {}
    with (
        patch(
            "scitex_agent_container.cli_pkg.send_cmds._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container.cli_pkg.send_cmds.subprocess.call",
            side_effect=lambda argv, cwd=None: captured.update(argv=argv) or 0,
        ),
    ):
        result = runner.invoke(
            send, ["alpha", "hi", "--model", "opus", "--max-turns", "3"]
        )
    assert result.exit_code == 0
    assert "--model" in captured["argv"]
    assert "opus" in captured["argv"]
    assert "--max-turns" in captured["argv"]
    assert "3" in captured["argv"]


def test_double_dash_forward_passes_args_through(isolated_env):
    runner = CliRunner()
    captured: dict = {}
    with (
        patch(
            "scitex_agent_container.cli_pkg.send_cmds._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container.cli_pkg.send_cmds.subprocess.call",
            side_effect=lambda argv, cwd=None: captured.update(argv=argv) or 0,
        ),
    ):
        # everything after the prompt becomes click's UNPROCESSED FORWARD
        result = runner.invoke(
            send,
            ["alpha", "hi", "--", "--dangerously-skip-permissions", "--debug"],
        )
    assert result.exit_code == 0, result.output
    assert "--dangerously-skip-permissions" in captured["argv"]
    assert "--debug" in captured["argv"]
