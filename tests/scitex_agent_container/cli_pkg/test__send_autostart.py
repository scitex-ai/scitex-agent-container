"""Tests for start-on-send: every send kicks, doctrine-guarded."""

from __future__ import annotations

from scitex_agent_container._reconcile._budget import Budget
from scitex_agent_container.cli_pkg._send_autostart import (
    maybe_autostart_for_send,
)


def _budget(history=None):
    return Budget(history=dict(history or {}))


def test_managed_crash_starts():
    # Arrange — managed policy, crashed (non-deliberate) exit.
    calls = []
    # Act
    started, note = maybe_autostart_for_send(
        "agent-a",
        policy_fn=lambda _n: "on-failure",
        row_fn=lambda _n: {"exit_reason": "pid_absent_at_sweep"},
        budget=_budget(),
        restart_fn=lambda n: calls.append(n) or True,
        record_fn=lambda *a: None,
        now=1000.0,
    )
    # Assert
    assert started is True
    assert calls == ["agent-a"]
    assert "auto-started" in note


def test_unmanaged_policy_never_starts():
    # Arrange — the default "never" policy means hands off.
    calls = []
    # Act
    started, note = maybe_autostart_for_send(
        "agent-a",
        policy_fn=lambda _n: "never",
        row_fn=lambda _n: {"exit_reason": "pid_absent_at_sweep"},
        budget=_budget(),
        restart_fn=lambda n: calls.append(n) or True,
        record_fn=lambda *a: None,
        now=1000.0,
    )
    # Assert
    assert started is False
    assert calls == []
    assert "not managed" in note


def test_deliberate_stop_is_sacred():
    # Arrange — `sac agents stop` wrote the exit reason.
    calls = []
    # Act
    started, note = maybe_autostart_for_send(
        "scitex-writer",
        policy_fn=lambda _n: "always",
        row_fn=lambda _n: {"exit_reason": "stopped"},
        budget=_budget(),
        restart_fn=lambda n: calls.append(n) or True,
        record_fn=lambda *a: None,
        now=1000.0,
    )
    # Assert
    assert started is False
    assert calls == []
    assert "sacred" in note


def test_budget_debounce_declines_loudly():
    # Arrange — restarted 10s ago; the debounce has not passed.
    calls = []
    # Act
    started, note = maybe_autostart_for_send(
        "agent-a",
        policy_fn=lambda _n: "always",
        row_fn=lambda _n: {"exit_reason": "reboot-swept"},
        budget=_budget({"agent-a": [9990.0]}),
        restart_fn=lambda n: calls.append(n) or True,
        record_fn=lambda *a: None,
        now=10000.0,
    )
    # Assert
    assert started is False
    assert calls == []
    assert "debounce" in note


def test_failed_restart_reports_failure():
    # Arrange — the restart itself reports failure.
    # Act
    started, note = maybe_autostart_for_send(
        "agent-a",
        policy_fn=lambda _n: "always",
        row_fn=lambda _n: None,
        budget=_budget(),
        restart_fn=lambda _n: False,
        record_fn=lambda *a: None,
        now=1000.0,
    )
    # Assert
    assert started is False
    assert "failure" in note


def test_never_observed_agent_with_managed_policy_starts():
    # Arrange — no instances row at all (never seen), managed policy.
    calls = []
    # Act
    started, _note = maybe_autostart_for_send(
        "agent-new",
        policy_fn=lambda _n: "always",
        row_fn=lambda _n: None,
        budget=_budget(),
        restart_fn=lambda n: calls.append(n) or True,
        record_fn=lambda *a: None,
        now=1000.0,
    )
    # Assert
    assert started is True
    assert calls == ["agent-new"]
