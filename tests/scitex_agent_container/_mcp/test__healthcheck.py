"""Tests for the MCP boot self-check + auto-heal.

The self-check parses ``claude mcp list`` output, logs the expected capability
surface, and — on a failed critical MCP — alarms + requests a rate-limited
self-restart. It must be FAIL-OPEN: never raise, always let boot proceed.

Conventions: one assert / AAA markers; no mocks / no monkeypatch — the
``claude mcp list`` runner and the restart requester are injected as REAL
callables, state lives under a real ``tmp_path``, and the env knobs are passed
as explicit parameters.
"""

from __future__ import annotations

from scitex_agent_container._mcp._healthcheck import (
    CONNECTED,
    FAILED,
    UNKNOWN,
    parse_mcp_status,
    run_healthcheck,
)

_SERVERS = ("scitex-agent-container", "scitex-todo")

_LIST_BOTH_OK = (
    "scitex-agent-container: sac mcp start - Connected\n"
    "scitex-todo: scitex-todo mcp start - Connected\n"
)
_LIST_TODO_FAILED = (
    "scitex-agent-container: sac mcp start - Connected\n"
    "scitex-todo: scitex-todo mcp start - Failed to connect\n"
)


class _RestartRecorder:
    """Real (non-mock) stand-in for the self-restart broker call."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.calls: list[str] = []

    def __call__(self, name: str) -> bool:
        self.calls.append(name)
        return self.accept


def test_parse_marks_connected_server():
    # Arrange
    text = _LIST_BOTH_OK
    # Act
    statuses = parse_mcp_status(text, _SERVERS)
    # Assert
    assert statuses["scitex-agent-container"] == CONNECTED


def test_parse_marks_failed_server():
    # Arrange
    text = _LIST_TODO_FAILED
    # Act
    statuses = parse_mcp_status(text, _SERVERS)
    # Assert
    assert statuses["scitex-todo"] == FAILED


def test_parse_marks_unmentioned_server_unknown():
    # Arrange — output mentions only one server.
    text = "scitex-agent-container: sac mcp start - Connected\n"
    # Act
    statuses = parse_mcp_status(text, _SERVERS)
    # Assert
    assert statuses["scitex-todo"] == UNKNOWN


def test_parse_empty_output_is_all_unknown():
    # Arrange
    text = ""
    # Act
    statuses = parse_mcp_status(text, _SERVERS)
    # Assert
    assert statuses["scitex-agent-container"] == UNKNOWN


def test_run_healthcheck_all_ok_action(tmp_path):
    # Arrange
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_BOTH_OK,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert result["action"] == "ok"


def test_run_healthcheck_all_ok_does_not_restart(tmp_path):
    # Arrange
    recorder = _RestartRecorder()
    # Act
    run_healthcheck(
        mcp_list_runner=lambda: _LIST_BOTH_OK,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert recorder.calls == []


def test_run_healthcheck_failed_reports_failed_server(tmp_path):
    # Arrange
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert result["failed"] == ["scitex-todo"]


def test_run_healthcheck_failed_requests_restart(tmp_path):
    # Arrange
    recorder = _RestartRecorder(accept=True)
    # Act
    run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert recorder.calls == ["agent-x"]


def test_run_healthcheck_restart_accepted_action(tmp_path):
    # Arrange
    recorder = _RestartRecorder(accept=True)
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert result["action"] == "restart-requested"


def test_run_healthcheck_second_call_hits_cooldown(tmp_path):
    # Arrange — first call stamps the sentinel; a near-in-time second must not
    # restart again.
    recorder = _RestartRecorder(accept=True)
    run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 200.0,
        state_dir=tmp_path,
    )
    # Assert
    assert result["action"] == "restart-cooldown"


def test_run_healthcheck_cooldown_does_not_restart_twice(tmp_path):
    # Arrange
    recorder = _RestartRecorder(accept=True)
    run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Act
    run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 200.0,
        state_dir=tmp_path,
    )
    # Assert — only the first call brokered a restart.
    assert recorder.calls == ["agent-x"]


def test_run_healthcheck_no_restart_alarms_only(tmp_path):
    # Arrange
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
        allow_restart=False,
    )
    # Assert
    assert result["action"] == "alarm-only"


def test_run_healthcheck_disabled(tmp_path):
    # Arrange
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: _LIST_TODO_FAILED,
        restart_fn=recorder,
        agent_name="agent-x",
        state_dir=tmp_path,
        disabled=True,
    )
    # Assert
    assert result["action"] == "disabled"


def test_run_healthcheck_fail_open_when_runner_raises(tmp_path):
    # Arrange — a runner that blows up must not crash the check.
    def _boom() -> str:
        raise RuntimeError("no claude binary")

    # Act
    result = run_healthcheck(
        mcp_list_runner=_boom,
        restart_fn=_RestartRecorder(),
        agent_name="agent-x",
        state_dir=tmp_path,
    )
    # Assert
    assert result["action"] == "error"
