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
    CRITICAL_CAPABILITIES,
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
    """A legacy-key failure is reported under the CANONICAL name.

    The input line still says ``scitex-todo`` (an un-migrated ``.mcp.json``),
    but the healthcheck reports every status under the preferred key so callers
    read ONE name regardless of which spelling the agent's config used. The
    failure is still detected — only its label is canonicalised.
    """
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
    assert result["failed"] == ["scitex-cards"]


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


# --------------------------------------------------------------------------
# Honest-UNKNOWN (coordinator dogfood 2026-07-09): when connectivity could NOT be
# verified (``claude mcp list`` unreadable/empty), the check must NEVER claim OK.
# A false-OK masks a client-side mid-session drop — worse than no check at all.
# --------------------------------------------------------------------------


def test_run_healthcheck_unreadable_list_is_unknown_not_ok(tmp_path):
    # Arrange — the runner returns "" (e.g. no `claude` binary in the container).
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: "",
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert — honest UNKNOWN, categorically NOT "ok".
    assert result["action"] == "unknown"


def test_run_healthcheck_unreadable_list_does_not_restart(tmp_path):
    # Arrange — UNKNOWN is not a confirmed failure, so it must not force a restart.
    recorder = _RestartRecorder(accept=True)
    # Act
    run_healthcheck(
        mcp_list_runner=lambda: "",
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert
    assert recorder.calls == []


def test_run_healthcheck_partial_unknown_is_not_ok(tmp_path):
    # Arrange — one server confirmed connected, the other absent from the output.
    one_connected = "scitex-agent-container: sac mcp start - Connected\n"
    recorder = _RestartRecorder()
    # Act
    result = run_healthcheck(
        mcp_list_runner=lambda: one_connected,
        restart_fn=recorder,
        agent_name="agent-x",
        now_fn=lambda: 100.0,
        state_dir=tmp_path,
    )
    # Assert — an unverified critical server means the whole reading is UNKNOWN.
    assert result["action"] == "unknown"


# ---------------------------------------------------------------------------
# scitex-todo -> scitex-cards rename: dual-name tolerance.
#
# The ``.mcp.json`` is NOT emitted by sac (it comes from the operator's to_home
# layers), so sac cannot flip the server key itself and a live fleet is rolled
# one agent at a time. Both spellings must therefore resolve to the SAME
# canonical status, or a not-yet-migrated agent's healthy board MCP reads as
# absent -> UNKNOWN -> a false alarm manufactured by the rename.
# ---------------------------------------------------------------------------

_CARDS = ("scitex-agent-container", "scitex-cards")

_LIST_NEW_KEY_OK = (
    "scitex-agent-container: sac mcp start - Connected\n"
    "scitex-cards: scitex-cards mcp start - Connected\n"
)
_LIST_OLD_KEY_OK = (
    "scitex-agent-container: sac mcp start - Connected\n"
    "scitex-todo: scitex-todo mcp start - Connected\n"
)
_LIST_OLD_KEY_FAILED = (
    "scitex-agent-container: sac mcp start - Connected\n"
    "scitex-todo: scitex-todo mcp start - Failed to connect\n"
)


def test_critical_capabilities_names_the_new_package():
    # Arrange
    keys = set(CRITICAL_CAPABILITIES)
    # Act
    present = "scitex-cards" in keys
    # Assert
    assert present


def test_new_server_key_resolves_connected():
    # Arrange
    text = _LIST_NEW_KEY_OK
    # Act
    statuses = parse_mcp_status(text, _CARDS)
    # Assert
    assert statuses["scitex-cards"] == CONNECTED


def test_legacy_server_key_still_resolves_under_the_new_name():
    """TOLERANCE: an un-migrated .mcp.json must not read as absent."""
    # Arrange
    text = _LIST_OLD_KEY_OK
    # Act
    statuses = parse_mcp_status(text, _CARDS)
    # Assert
    assert statuses["scitex-cards"] == CONNECTED


def test_legacy_server_key_failure_is_reported_under_the_new_name():
    """Tolerance must not swallow a real failure on the old key."""
    # Arrange
    text = _LIST_OLD_KEY_FAILED
    # Act
    statuses = parse_mcp_status(text, _CARDS)
    # Assert
    assert statuses["scitex-cards"] == FAILED


def test_genuinely_absent_board_server_is_still_unknown():
    """Tolerance must not manufacture a status out of nothing."""
    # Arrange — neither spelling appears.
    text = "scitex-agent-container: sac mcp start - Connected\n"
    # Act
    statuses = parse_mcp_status(text, _CARDS)
    # Assert
    assert statuses["scitex-cards"] == UNKNOWN


def test_caller_asking_for_the_legacy_name_verbatim_gets_that_key():
    """Back-compat: an explicit legacy request keeps its own key in the result."""
    # Arrange
    text = _LIST_OLD_KEY_OK
    # Act
    statuses = parse_mcp_status(text, ("scitex-todo",))
    # Assert
    assert statuses["scitex-todo"] == CONNECTED
