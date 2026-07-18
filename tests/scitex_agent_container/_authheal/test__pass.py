"""One login-expired auto-restart pass, end to end, with real state.

Detection (the real ``evaluate_agents`` corroboration) runs against injected
panes; the ONE irreversible act (the restart) is a recording callable, and
the budget/history/cards are real on-disk state. No mocks of anything under
test.

The load-bearing safety property is that a persistently-failing agent is
CARDED, never bounced forever: these tests pin the debounce, the hourly cap,
and the escalation card that replaces an infinite restart loop.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._authheal._alarm import CARD_ID_PREFIX, card_id_for
from scitex_agent_container._authheal._pass import auth_heal_pass
from scitex_agent_container._reconcile._budget import (
    DEBOUNCE_S,
    MAX_RESTARTS_PER_AGENT_PER_HOUR,
)
from scitex_agent_container._reconcile._rule import Verdict

from ._helpers import NOW, Recorder, stuck, transient


def _seed_history(path: Path, mapping: dict) -> None:
    path.write_text(json.dumps(mapping))


def _run(capture, history, store, rec, **over):
    kwargs = dict(
        apply=True,
        alarm=False,
        now=NOW,
        history_file=history,
        store=store,
        capture_fn=lambda: capture,
        restart_fn=rec,
    )
    kwargs.update(over)
    return auth_heal_pass(**kwargs)


def _verdict(outcome, name: str) -> Verdict:
    return next(r.verdict for r in outcome.reports if r.name == name)


# --- the happy path: a corroborated wedge is restarted --------------------


def test_corroborated_login_expired_agent_is_restarted(history, store):
    # Arrange — one frozen-banner agent, empty history (first sight).
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec)
    # Assert — the single irreversible act happened, exactly once, for it.
    assert rec.names == ["hpc"]


def test_corroborated_agent_reports_restarted(history, store):
    # Arrange
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), history, store, rec)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.RESTARTED


# --- the safety gate: a transient / single-run flag is NOT restarted ------


def test_single_run_transient_agent_is_not_restarted(history, store):
    # Arrange — a banner on run 1, gone on the decisive run 2: not frozen.
    rec = Recorder()
    # Act
    _run(transient("figrecipe"), history, store, rec)
    # Assert — no restart, because it was never corroborated.
    assert rec.names == []


# --- dry-run / --check: report, never act ---------------------------------


def test_check_mode_does_not_restart(history, store):
    # Arrange — apply=False is the dry-run the `--check` flag selects.
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec, apply=False)
    # Assert
    assert rec.names == []


def test_check_mode_reports_would_restart(history, store):
    # Arrange
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), history, store, rec, apply=False)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.WOULD_RESTART


# --- the debounce bound: a just-restarted agent is left to boot ------------


def test_agent_inside_the_debounce_is_not_restarted_again(history, store):
    # Arrange — restarted 100s ago; still login-expired, but restarting again
    # now would kill the recovery in progress.
    _seed_history(history, {"hpc": [NOW - 100]})
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec)
    # Assert
    assert rec.names == []


def test_agent_inside_the_debounce_reports_cooling_down(history, store):
    # Arrange
    _seed_history(history, {"hpc": [NOW - 100]})
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), history, store, rec)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.COOLING_DOWN


# --- the hourly cap: persistently failing → card, NOT an infinite bounce ---


def test_agent_over_the_hourly_cap_is_not_restarted(history, store):
    # Arrange — already restarted MAX_RESTARTS_PER_AGENT_PER_HOUR times inside
    # the rolling hour and the debounce has since passed; it is STILL wedged.
    # Restarting is not fixing it — a loop is worse than a down agent. Two
    # stamps within the hour, the newest older than the debounce.
    stamps = [NOW - 3400, NOW - int(DEBOUNCE_S) - 100][:MAX_RESTARTS_PER_AGENT_PER_HOUR]
    _seed_history(history, {"hpc": stamps})
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec)
    # Assert — the loop is refused.
    assert rec.names == []


def test_agent_over_the_hourly_cap_reports_over_budget(history, store):
    # Arrange
    _seed_history(history, {"hpc": [NOW - 3400, NOW - int(DEBOUNCE_S) - 100]})
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), history, store, rec)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.OVER_BUDGET


def test_over_budget_agent_is_escalated_to_a_board_card(history, store):
    # Arrange — the whole point of the cap: instead of bouncing forever, the
    # agent that cannot be healed is handed to a human via an idempotent card.
    from scitex_todo import list_tasks

    _seed_history(history, {"hpc": [NOW - 3400, NOW - int(DEBOUNCE_S) - 100]})
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec, alarm=True)
    # Assert — exactly one escalation card, for this agent.
    ids = [c["id"] for c in list_tasks(store, id_prefix=CARD_ID_PREFIX)]
    assert ids == [card_id_for("hpc")]


# --- can't read our own memory → refuse, never a blind loop ---------------


def test_unreadable_budget_refuses_to_restart(denied_history, store):
    # Arrange — the restart history cannot be created, so the debounce/cap are
    # unenforceable. Restarting anyway would make every wedge restartable on
    # every tick, forever — the exact loop the budget exists to prevent.
    rec = Recorder()
    # Act
    _run(stuck("hpc"), denied_history, store, rec)
    # Assert
    assert rec.names == []


def test_unreadable_budget_reports_budget_unknown(denied_history, store):
    # Arrange
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), denied_history, store, rec)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.BUDGET_UNKNOWN
