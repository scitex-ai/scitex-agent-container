"""Tests for ``cli_pkg.hook_cmds`` (ingest-hook-event handler).

PA-306 no-mocks. Every test exercises real production code:

* ``hook_event`` reads real stdin piped via Click's ``CliRunner(input=)``
  or a real ``subprocess.run(..., input=...)`` child process.
* Events are appended to a real on-disk JSONL ring-buffer rooted under
  ``tmp_path`` via the documented ``SCITEX_DIR`` env var, with cwd
  redirected outside any git repo so the local-state cascade selects
  the user scope (``$SCITEX_DIR/agent-container/runtime/events/``).
* ``_resolve_agent`` is tested against real ``os.environ`` and real
  ``Path.cwd()`` — no callable patches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.hook_cmds import (
    _resolve_agent,
    hook_event,
)

# ---------------------------------------------------------------------------
# Real-collaborator fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_log_root(tmp_path: Path, env_save_restore):
    """Redirect the event log to ``tmp_path`` via real env + cwd seams.

    ``$SCITEX_DIR`` controls the user-scope root for the local-state
    cascade. cwd is moved to ``tmp_path`` (outside any git repo) so
    ``find_project_scope`` returns ``None`` and the user scope wins.
    Returns the directory where ``<agent>.jsonl`` files will be written.
    """
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "scitex_home"))
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.delete("CLAUDE_AGENT_ID")
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path / "scitex_home" / "agent-container" / "runtime" / "events"
    finally:
        os.chdir(saved_cwd)


# ---------------------------------------------------------------------------
# _resolve_agent — real env + cwd
# ---------------------------------------------------------------------------


def test_resolve_agent_prefers_flag(env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_AGENT", "from-env")
    # Act
    resolved = _resolve_agent("from-flag")
    # Assert
    assert resolved == "from-flag"


def test_resolve_agent_uses_primary_env(env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_AGENT", "primary-env-agent")
    env_save_restore.delete("CLAUDE_AGENT_ID")
    # Act
    resolved = _resolve_agent("")
    # Assert
    assert resolved == "primary-env-agent"


def test_resolve_agent_falls_back_to_claude_env(env_save_restore):
    # Arrange
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.set("CLAUDE_AGENT_ID", "claude-fallback")
    # Act
    resolved = _resolve_agent("")
    # Assert
    assert resolved == "claude-fallback"


def test_resolve_agent_uses_cwd_basename(env_save_restore, tmp_path):
    # Arrange
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.delete("CLAUDE_AGENT_ID")
    agent_dir = tmp_path / "cwd-based-agent"
    agent_dir.mkdir()
    saved_cwd = os.getcwd()
    os.chdir(agent_dir)
    try:
        # Act
        resolved = _resolve_agent("")
    finally:
        os.chdir(saved_cwd)
    # Assert
    assert resolved == "cwd-based-agent"


# ---------------------------------------------------------------------------
# hook_event — Click runner with real stdin
# ---------------------------------------------------------------------------


def test_hook_event_writes_pretool_record(event_log_root: Path):
    # Arrange
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
    )
    # Act
    result = CliRunner().invoke(
        hook_event, ["pretool", "--agent", "alpha"], input=payload
    )
    # Assert
    assert result.exit_code == 0


def test_hook_event_creates_agent_log_file(event_log_root: Path):
    # Arrange
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    # Act
    CliRunner().invoke(hook_event, ["pretool", "--agent", "beta-agent"], input=payload)
    # Assert
    assert (event_log_root / "beta-agent.jsonl").is_file()


def test_hook_event_records_tool_name(event_log_root: Path):
    # Arrange
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "TODO"}})
    # Act
    CliRunner().invoke(hook_event, ["pretool", "--agent", "gamma"], input=payload)
    record = json.loads((event_log_root / "gamma.jsonl").read_text().splitlines()[0])
    # Assert
    assert record["tool"] == "Grep"


def test_hook_event_records_kind_lowercase(event_log_root: Path):
    # Arrange
    payload = json.dumps({})
    # Act
    CliRunner().invoke(hook_event, ["STOP", "--agent", "delta"], input=payload)
    record = json.loads((event_log_root / "delta.jsonl").read_text().splitlines()[0])
    # Assert
    assert record["kind"] == "stop"


def test_hook_event_handles_prompt_payload(event_log_root: Path):
    # Arrange
    payload = json.dumps({"prompt": "hello there"})
    # Act
    CliRunner().invoke(hook_event, ["prompt", "--agent", "epsilon"], input=payload)
    record = json.loads((event_log_root / "epsilon.jsonl").read_text().splitlines()[0])
    # Assert
    assert record["prompt_preview"] == "hello there"


def test_hook_event_tolerates_malformed_json(event_log_root: Path):
    # Arrange
    bad_payload = "{not valid json"
    # Act
    result = CliRunner().invoke(
        hook_event, ["other", "--agent", "zeta"], input=bad_payload
    )
    # Assert
    assert result.exit_code == 0


def test_hook_event_writes_log_for_malformed_json(event_log_root: Path):
    # Arrange
    bad_payload = "totally not json at all"
    # Act
    CliRunner().invoke(hook_event, ["other", "--agent", "eta"], input=bad_payload)
    # Assert
    assert (event_log_root / "eta.jsonl").is_file()


def test_hook_event_handles_empty_stdin(event_log_root: Path):
    # Arrange
    empty = ""
    # Act
    result = CliRunner().invoke(
        hook_event, ["pretool", "--agent", "theta"], input=empty
    )
    # Assert
    assert result.exit_code == 0


def test_hook_event_uses_env_agent_fallback(event_log_root: Path, env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_AGENT", "env-resolved")
    payload = json.dumps({"tool_name": "Read", "tool_input": {}})
    # Act
    CliRunner().invoke(hook_event, ["pretool"], input=payload)
    # Assert
    assert (event_log_root / "env-resolved.jsonl").is_file()


def test_hook_event_appends_subsequent_calls(event_log_root: Path):
    # Arrange
    runner = CliRunner()
    runner.invoke(
        hook_event,
        ["pretool", "--agent", "iota"],
        input=json.dumps({"tool_name": "Read", "tool_input": {}}),
    )
    # Act
    runner.invoke(
        hook_event,
        ["pretool", "--agent", "iota"],
        input=json.dumps({"tool_name": "Grep", "tool_input": {}}),
    )
    lines = (event_log_root / "iota.jsonl").read_text().splitlines()
    # Assert
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# hook_event — real subprocess (real OS-level stdin pipe)
# ---------------------------------------------------------------------------


def test_hook_event_via_subprocess_stdin_pipe(tmp_path: Path, env_save_restore):
    # Arrange
    home = tmp_path / "scitex_home"
    env = {
        **os.environ,
        "SCITEX_DIR": str(home),
        "SCITEX_AGENT_CONTAINER_AGENT": "subproc-agent",
    }
    env.pop("CLAUDE_AGENT_ID", None)
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
    # Act
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scitex_agent_container",
            "event",
            "ingest",
            "pretool",
        ],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    # Assert
    assert completed.returncode == 0


def test_hook_event_subprocess_writes_real_log(
    tmp_path: Path,
):
    # Arrange
    home = tmp_path / "scitex_home"
    env = {
        **os.environ,
        "SCITEX_DIR": str(home),
        "SCITEX_AGENT_CONTAINER_AGENT": "real-pipe-agent",
    }
    env.pop("CLAUDE_AGENT_ID", None)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
    # Act
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scitex_agent_container",
            "event",
            "ingest",
            "pretool",
        ],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        check=False,
    )
    log = home / "agent-container" / "runtime" / "events" / "real-pipe-agent.jsonl"
    # Assert
    assert log.is_file()
