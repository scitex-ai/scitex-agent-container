"""Tests for ``sac agent send``.

Covers the resume-path wrapper from SAC_OROCHI_SCOPES.md §6 step 1:
read session_id → cd workdir → exec claude --resume <sid> -p.

PA-306: no ``unittest.mock``. Production collaborators are swapped at
the module namespace via ``_swap`` context managers, and env mutations
go through explicit save/restore.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.send_cmds as send_mod
from scitex_agent_container.cli_pkg.send_cmds import send


@contextmanager
def _swap(name: str, fn: Callable) -> Iterator[None]:
    saved = getattr(send_mod, name)
    setattr(send_mod, name, fn)
    try:
        yield
    finally:
        setattr(send_mod, name, saved)


@contextmanager
def _swap_subprocess_call(fn: Callable) -> Iterator[None]:
    saved = send_mod.subprocess.call
    send_mod.subprocess.call = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        send_mod.subprocess.call = saved  # type: ignore[assignment]


@contextmanager
def _swap_os_kill(fn: Callable) -> Iterator[None]:
    saved = send_mod.os.kill
    send_mod.os.kill = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        send_mod.os.kill = saved  # type: ignore[assignment]


def _seed_agent(tmp_path: Path, name: str, session_id: str) -> Path:
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
def isolated_env(tmp_path):
    """PA-306: env + send_mod.state_dir_for save/restore in one fixture."""
    yaml_root = _seed_agent(tmp_path, "alpha", "abc-123-def")
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved_env = os.environ.get(key)
    saved_state = send_mod.state_dir_for
    os.environ[key] = str(yaml_root)
    send_mod.state_dir_for = (  # type: ignore[assignment]
        lambda name, root=None: tmp_path / "state" / name
    )
    try:
        yield tmp_path
    finally:
        send_mod.state_dir_for = saved_state  # type: ignore[assignment]
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env


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


def test_key_esc_sends_sigint(isolated_env):
    (isolated_env / "state" / "alpha" / "pid").write_text("4242")
    killed: dict = {}
    with _swap_os_kill(lambda pid, sig: killed.update(pid=pid, sig=sig)):
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


def test_missing_session_id_errors_clearly(tmp_path):
    yaml_root = _seed_agent(tmp_path, "alpha", "sid-1")
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved_env = os.environ.get(key)
    os.environ[key] = str(yaml_root)
    saved_state = send_mod.state_dir_for
    send_mod.state_dir_for = (  # type: ignore[assignment]
        lambda name, root=None: tmp_path / "state" / name
    )
    # remove the session_id file
    (tmp_path / "state" / "alpha" / "session_id").unlink()
    try:
        with _swap("_find_claude_binary", lambda: "/usr/bin/true"):
            runner = CliRunner()
            result = runner.invoke(send, ["alpha", "hi"])
        assert result.exit_code != 0
        assert "No session_id recorded" in result.output
    finally:
        send_mod.state_dir_for = saved_state  # type: ignore[assignment]
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env


def test_happy_path_invokes_claude_with_resume_in_workdir(isolated_env):
    runner = CliRunner()
    captured: dict = {}

    def fake_call(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return 0

    with (
        _swap("_find_claude_binary", lambda: "/usr/local/bin/claude"),
        _swap_subprocess_call(fake_call),
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
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
    ):
        result = runner.invoke(send, ["alpha", "hello", "--no-stream"])
    assert result.exit_code == 0
    assert "stream-json" not in captured["argv"]
    assert "--output-format" not in captured["argv"]


def test_model_and_max_turns_forwarded(isolated_env):
    runner = CliRunner()
    captured: dict = {}
    with (
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
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
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
    ):
        result = runner.invoke(
            send,
            ["alpha", "hi", "--", "--dangerously-skip-permissions", "--debug"],
        )
    assert result.exit_code == 0, result.output
    assert "--dangerously-skip-permissions" in captured["argv"]
    assert "--debug" in captured["argv"]
