"""Tests for ``sac agent send``.

Covers the resume-path wrapper from SAC_OROCHI_SCOPES.md §6 step 1:
read session_id → cd workdir → exec claude --resume <sid> -p.

PA-306: no ``unittest.mock``. Production collaborators are swapped at
the module namespace via ``_swap`` context managers, and env mutations
go through explicit save/restore.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), and asserts exactly one
fact (TQ007). Shared invariants over equivalent inputs are collapsed
into ``pytest.parametrize`` so the matrix stays declarative.
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
    """PA-306: env + send_mod.state_dir_for save/restore in one fixture.

    Also pins ``resolve_config`` to the seeded yaml root so a pre-existing
    ``~/.scitex/agent-container/agents/<name>/spec.yaml`` on the dev box
    cannot shadow the per-test fixture (resolver search order puts the
    home install root before ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``).
    """
    yaml_root = _seed_agent(tmp_path, "alpha", "abc-123-def")
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved_env = os.environ.get(key)
    saved_state = send_mod.state_dir_for
    saved_resolve = send_mod.resolve_config
    os.environ[key] = str(yaml_root)
    send_mod.state_dir_for = (  # type: ignore[assignment]
        lambda name, root=None: tmp_path / "state" / name
    )
    send_mod.resolve_config = (  # type: ignore[assignment]
        lambda name: str(yaml_root / name / "spec.yaml")
    )
    try:
        yield tmp_path
    finally:
        send_mod.resolve_config = saved_resolve  # type: ignore[assignment]
        send_mod.state_dir_for = saved_state  # type: ignore[assignment]
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env


# ---------------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------------


def test_invocation_without_prompt_or_key_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha"])
    # Assert
    assert result.exit_code != 0


def test_invocation_without_prompt_or_key_reports_requirement_in_output(
    isolated_env,
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha"])
    # Assert
    assert "Either PROMPT or --key is required" in result.output


def test_invocation_with_both_prompt_and_key_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "hello", "--key", "ESC"])
    # Assert
    assert result.exit_code != 0


def test_invocation_with_both_prompt_and_key_reports_mutual_exclusion(
    isolated_env,
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "hello", "--key", "ESC"])
    # Assert
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# --key ESC: SIGINT delivery
# ---------------------------------------------------------------------------


@pytest.fixture
def alpha_with_pid(isolated_env):
    """isolated_env plus a recorded pid file for the alpha agent."""
    (isolated_env / "state" / "alpha" / "pid").write_text("4242")
    return isolated_env


def _invoke_key_esc_capturing_kill():
    """Run ``send alpha --key ESC`` and return (result, kill_call)."""
    kill_call: dict = {}
    with _swap_os_kill(lambda pid, sig: kill_call.update(pid=pid, sig=sig)):
        runner = CliRunner()
        result = runner.invoke(send, ["alpha", "--key", "ESC"])
    return result, kill_call


def test_key_esc_with_recorded_pid_exits_zero(alpha_with_pid):
    # Arrange
    invoke = _invoke_key_esc_capturing_kill
    # Act
    result, _ = invoke()
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "field,expected",
    [
        ("pid", 4_242),
        ("sig", 2),  # signal.SIGINT
    ],
)
def test_key_esc_delivers_sigint_to_recorded_pid(alpha_with_pid, field, expected):
    # Arrange
    invoke = _invoke_key_esc_capturing_kill
    # Act
    _, kill_call = invoke()
    # Assert
    assert kill_call[field] == expected


def test_key_unsupported_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "F12"])
    # Assert
    assert result.exit_code != 0


def test_key_unsupported_reports_not_supported(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "F12"])
    # Assert
    assert "not supported" in result.output


def test_key_esc_without_pid_file_exits_nonzero(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    # Assert
    assert result.exit_code != 0


def test_key_esc_without_pid_file_reports_not_running(isolated_env):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alpha", "--key", "ESC"])
    # Assert
    assert "not running" in result.output


# ---------------------------------------------------------------------------
# Missing session_id
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env_without_session_id(tmp_path):
    """isolated_env, but with the session_id file deleted before yield."""
    yaml_root = _seed_agent(tmp_path, "alpha", "sid-1")
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    saved_env = os.environ.get(key)
    saved_state = send_mod.state_dir_for
    saved_resolve = send_mod.resolve_config
    os.environ[key] = str(yaml_root)
    send_mod.state_dir_for = (  # type: ignore[assignment]
        lambda name, root=None: tmp_path / "state" / name
    )
    send_mod.resolve_config = (  # type: ignore[assignment]
        lambda name: str(yaml_root / name / "spec.yaml")
    )
    (tmp_path / "state" / "alpha" / "session_id").unlink()
    try:
        yield tmp_path
    finally:
        send_mod.resolve_config = saved_resolve  # type: ignore[assignment]
        send_mod.state_dir_for = saved_state  # type: ignore[assignment]
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env


def _invoke_send_without_session_id():
    """Run ``send alpha hi`` against a missing session_id and return result."""
    with _swap("_find_claude_binary", lambda: "/usr/bin/true"):
        runner = CliRunner()
        return runner.invoke(send, ["alpha", "hi"])


def test_missing_session_id_exits_nonzero(isolated_env_without_session_id):
    # Arrange
    invoke = _invoke_send_without_session_id
    # Act
    result = invoke()
    # Assert
    assert result.exit_code != 0


def test_missing_session_id_reports_no_session_recorded(
    isolated_env_without_session_id,
):
    # Arrange
    invoke = _invoke_send_without_session_id
    # Act
    result = invoke()
    # Assert
    assert "No session_id recorded" in result.output


# ---------------------------------------------------------------------------
# Happy path: resume invocation
# ---------------------------------------------------------------------------


def _invoke_happy_path():
    """Run ``send alpha 'follow up please'`` and capture argv/cwd."""
    captured: dict = {}

    def fake_call(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return 0

    with (
        _swap("_find_claude_binary", lambda: "/usr/local/bin/claude"),
        _swap_subprocess_call(fake_call),
    ):
        captured["result"] = CliRunner().invoke(send, ["alpha", "follow up please"])
    return captured


def test_happy_path_exits_zero(isolated_env):
    # Arrange
    invoke = _invoke_happy_path
    # Act
    captured = invoke()
    # Assert
    assert captured["result"].exit_code == 0, captured["result"].output


def test_happy_path_invokes_claude_with_resume_prefix(isolated_env):
    # Arrange
    invoke = _invoke_happy_path
    # Act
    captured = invoke()
    # Assert
    assert captured["argv"][:5] == [
        "/usr/local/bin/claude",
        "--resume",
        "abc-123-def",
        "-p",
        "follow up please",
    ]


@pytest.mark.parametrize(
    "flag",
    ["--output-format", "stream-json"],
)
def test_happy_path_argv_contains_streaming_flag(isolated_env, flag):
    # Arrange
    invoke = _invoke_happy_path
    # Act
    captured = invoke()
    # Assert
    assert flag in captured["argv"]


def test_happy_path_runs_in_agent_workdir(isolated_env):
    # Arrange
    expected_cwd = str(isolated_env / "workdir")
    # Act
    captured = _invoke_happy_path()
    # Assert
    assert expected_cwd == captured["cwd"]


# ---------------------------------------------------------------------------
# --no-stream
# ---------------------------------------------------------------------------


def _invoke_no_stream():
    """Run ``send alpha hello --no-stream`` and capture argv/result."""
    captured: dict = {}
    with (
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
    ):
        captured["result"] = CliRunner().invoke(send, ["alpha", "hello", "--no-stream"])
    return captured


def test_no_stream_exits_zero(isolated_env):
    # Arrange
    invoke = _invoke_no_stream
    # Act
    captured = invoke()
    # Assert
    assert captured["result"].exit_code == 0


@pytest.mark.parametrize(
    "flag",
    ["stream-json", "--output-format"],
)
def test_no_stream_strips_streaming_flag(isolated_env, flag):
    # Arrange
    invoke = _invoke_no_stream
    # Act
    captured = invoke()
    # Assert
    assert flag not in captured["argv"]


# ---------------------------------------------------------------------------
# --model / --max-turns forwarding
# ---------------------------------------------------------------------------


def _invoke_model_and_max_turns():
    """Run ``send alpha hi --model opus --max-turns 3`` and capture argv."""
    captured: dict = {}
    with (
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
    ):
        captured["result"] = CliRunner().invoke(
            send, ["alpha", "hi", "--model", "opus", "--max-turns", "3"]
        )
    return captured


def test_model_and_max_turns_invocation_exits_zero(isolated_env):
    # Arrange
    invoke = _invoke_model_and_max_turns
    # Act
    captured = invoke()
    # Assert
    assert captured["result"].exit_code == 0


@pytest.mark.parametrize(
    "token",
    ["--model", "opus", "--max-turns", "3"],
)
def test_model_and_max_turns_forwarded_in_argv(isolated_env, token):
    # Arrange
    invoke = _invoke_model_and_max_turns
    # Act
    captured = invoke()
    # Assert
    assert token in captured["argv"]


# ---------------------------------------------------------------------------
# Trailing ``--`` passthrough
# ---------------------------------------------------------------------------


def _invoke_double_dash_passthrough():
    """Run ``send alpha hi -- --dangerously-skip-permissions --debug``."""
    captured: dict = {}
    with (
        _swap("_find_claude_binary", lambda: "/x/claude"),
        _swap_subprocess_call(lambda argv, cwd=None: captured.update(argv=argv) or 0),
    ):
        captured["result"] = CliRunner().invoke(
            send,
            ["alpha", "hi", "--", "--dangerously-skip-permissions", "--debug"],
        )
    return captured


def test_double_dash_invocation_exits_zero(isolated_env):
    # Arrange
    invoke = _invoke_double_dash_passthrough
    # Act
    captured = invoke()
    # Assert
    assert captured["result"].exit_code == 0, captured["result"].output


@pytest.mark.parametrize(
    "passthrough_arg",
    ["--dangerously-skip-permissions", "--debug"],
)
def test_double_dash_forwards_extra_arg_to_argv(isolated_env, passthrough_arg):
    # Arrange
    invoke = _invoke_double_dash_passthrough
    # Act
    captured = invoke()
    # Assert
    assert passthrough_arg in captured["argv"]
