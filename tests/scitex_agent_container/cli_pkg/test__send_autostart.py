"""Tests for start-on-send: every send kicks, doctrine-guarded."""

from __future__ import annotations

from scitex_agent_container._reconcile._budget import Budget
from scitex_agent_container.cli_pkg._send_autostart import (
    maybe_autostart_for_send,
)


def _budget(history=None):
    return Budget(history=dict(history or {}))


def _autostart(
    *,
    agent="agent-a",
    policy="always",
    row=None,
    history=None,
    restart_result=True,
    now=1000.0,
):
    """Run the Act step once; return (started, note, restart calls)."""
    calls = []
    started, note = maybe_autostart_for_send(
        agent,
        policy_fn=lambda _n: policy,
        row_fn=lambda _n: row,
        budget=_budget(history),
        restart_fn=lambda n: calls.append(n) or restart_result,
        record_fn=lambda *a: None,
        now=now,
    )
    return started, note, calls


def test_managed_crash_reports_started():
    # Arrange — managed policy, crashed (non-deliberate) exit.
    # Act
    started, _note, _calls = _autostart(
        policy="on-failure", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert started is True


def test_managed_crash_restarts_the_sending_agent():
    # Arrange — the same crashed agent under a managed policy.
    # Act
    _started, _note, calls = _autostart(
        policy="on-failure", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert calls == ["agent-a"]


def test_managed_crash_note_reports_auto_start():
    # Arrange — the same crashed agent under a managed policy.
    # Act
    _started, note, _calls = _autostart(
        policy="on-failure", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert "auto-started" in note


def test_unmanaged_policy_reports_not_started():
    # Arrange — the default "never" policy means hands off.
    # Act
    started, _note, _calls = _autostart(
        policy="never", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert started is False


def test_unmanaged_policy_issues_no_restart():
    # Arrange — the same unmanaged agent; restart must stay untouched.
    # Act
    _started, _note, calls = _autostart(
        policy="never", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert calls == []


def test_unmanaged_policy_note_reports_not_managed():
    # Arrange — the same unmanaged agent; the note must say why.
    # Act
    _started, note, _calls = _autostart(
        policy="never", row={"exit_reason": "pid_absent_at_sweep"}
    )
    # Assert
    assert "not managed" in note


def test_deliberate_stop_reports_not_started():
    # Arrange — `sac agents stop` wrote the exit reason.
    # Act
    started, _note, _calls = _autostart(
        agent="scitex-writer", policy="always", row={"exit_reason": "stopped"}
    )
    # Assert
    assert started is False


def test_deliberate_stop_issues_no_restart():
    # Arrange — the same deliberately stopped agent stays down.
    # Act
    _started, _note, calls = _autostart(
        agent="scitex-writer", policy="always", row={"exit_reason": "stopped"}
    )
    # Assert
    assert calls == []


def test_deliberate_stop_note_marks_stop_sacred():
    # Arrange — the same deliberately stopped agent; the note must say why.
    # Act
    _started, note, _calls = _autostart(
        agent="scitex-writer", policy="always", row={"exit_reason": "stopped"}
    )
    # Assert
    assert "sacred" in note


def test_budget_debounce_reports_not_started():
    # Arrange — restarted 10s ago; the debounce has not passed.
    # Act
    started, _note, _calls = _autostart(
        policy="always",
        row={"exit_reason": "reboot-swept"},
        history={"agent-a": [9990.0]},
        now=10000.0,
    )
    # Assert
    assert started is False


def test_budget_debounce_issues_no_restart():
    # Arrange — the same debounced agent; restart must stay untouched.
    # Act
    _started, _note, calls = _autostart(
        policy="always",
        row={"exit_reason": "reboot-swept"},
        history={"agent-a": [9990.0]},
        now=10000.0,
    )
    # Assert
    assert calls == []


def test_budget_debounce_note_names_debounce():
    # Arrange — the same debounced agent; the note must say why.
    # Act
    _started, note, _calls = _autostart(
        policy="always",
        row={"exit_reason": "reboot-swept"},
        history={"agent-a": [9990.0]},
        now=10000.0,
    )
    # Assert
    assert "debounce" in note


def test_failed_restart_reports_not_started():
    # Arrange — the restart itself reports failure.
    # Act
    started, _note, _calls = _autostart(
        policy="always", row=None, restart_result=False
    )
    # Assert
    assert started is False


def test_failed_restart_note_reports_failure():
    # Arrange — the same failed restart; the note must say why.
    # Act
    _started, note, _calls = _autostart(
        policy="always", row=None, restart_result=False
    )
    # Assert
    assert "failure" in note


def test_never_observed_managed_agent_reports_started():
    # Arrange — no instances row at all (never seen), managed policy.
    # Act
    started, _note, _calls = _autostart(agent="agent-new", policy="always", row=None)
    # Assert
    assert started is True


def test_never_observed_managed_agent_restarts_by_name():
    # Arrange — the same never-seen agent under a managed policy.
    # Act
    _started, _note, calls = _autostart(agent="agent-new", policy="always", row=None)
    # Assert
    assert calls == ["agent-new"]
