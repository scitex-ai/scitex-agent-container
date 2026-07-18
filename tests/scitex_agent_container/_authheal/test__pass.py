"""One login-expired auto-restart pass, end to end, with real state.

Detection (the real ``evaluate_agents`` corroboration) runs against injected
panes; the ONE irreversible act (the restart) is a recording callable, and
the budget/history/cards are real on-disk state. No mocks of anything under
test.

The load-bearing safety property is that a persistently-failing agent is
CARDED, never bounced forever: these tests pin the debounce, the hourly cap,
and the escalation card that replaces an infinite restart loop.

The second load-bearing property is that a pass cannot report a clean fleet it
did not look at. An agent whose pane will not capture, and a registered agent
with no session at all, must both leave a VISIBLE report and drive the exit
code to could-not-determine — while still never being restarted, because an
unread pane is no evidence of a wedge. Exit 0 is reserved for a roster fully
accounted for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._authheal._alarm import CARD_ID_PREFIX, card_id_for
from scitex_agent_container._authheal._pass import auth_heal_pass
from scitex_agent_container._reconcile._budget import (
    DEBOUNCE_S,
    MAX_RESTARTS_PER_AGENT_PER_HOUR,
    save_history,
)
from scitex_agent_container._reconcile._rule import Verdict

from ._helpers import NOW, OK, Recorder, register_agents, stuck, transient


def _seed_history(path: Path, mapping: dict) -> None:
    # Seed via the PRODUCTION writer so the file is byte-identical to what a real
    # pass persists (every seed stamp is inside the 1h window, so none prunes).
    save_history(path, mapping, now=NOW)


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
    stamps = [NOW - 3_400, NOW - int(DEBOUNCE_S) - 100][
        :MAX_RESTARTS_PER_AGENT_PER_HOUR
    ]
    _seed_history(history, {"hpc": stamps})
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec)
    # Assert — the loop is refused.
    assert rec.names == []


def test_agent_over_the_hourly_cap_reports_over_budget(history, store):
    # Arrange
    _seed_history(history, {"hpc": [NOW - 3_400, NOW - int(DEBOUNCE_S) - 100]})
    rec = Recorder()
    # Act
    outcome = _run(stuck("hpc"), history, store, rec)
    # Assert
    assert _verdict(outcome, "hpc") == Verdict.OVER_BUDGET


def test_over_budget_agent_is_escalated_to_a_board_card(history, store):
    # Arrange — the whole point of the cap: instead of bouncing forever, the
    # agent that cannot be healed is handed to a human via an idempotent card.
    # This ONE test needs the real board writer; skip cleanly where the CI test
    # SIF has not layered scitex_todo in (the rest of this module does not).
    scitex_todo = pytest.importorskip("scitex_todo")
    _seed_history(history, {"hpc": [NOW - 3_400, NOW - int(DEBOUNCE_S) - 100]})
    rec = Recorder()
    # Act
    _run(stuck("hpc"), history, store, rec, alarm=True)
    # Assert — exactly one escalation card, for this agent.
    ids = [c["id"] for c in scitex_todo.list_tasks(store, id_prefix=CARD_ID_PREFIX)]
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


# --- an agent we did NOT read is REPORTED, never silently dropped ----------
#
# The pane of a live agent will not capture, so the pass learns nothing at all
# about its auth. Producing no report for it made "we checked and it is fine"
# and "we never looked" the same answer — which is how a wedged agent sat for
# hours while every pass logged a success.


def test_uncapturable_pane_is_reported(history, store):
    # Arrange — a live session whose pane cannot be read.
    rec = Recorder()
    # Act
    outcome = _run({"scitex-hub": (None, None)}, history, store, rec)
    # Assert
    assert _verdict(outcome, "scitex-hub") == Verdict.UNOBSERVED


def test_uncapturable_pane_makes_the_pass_could_not_determine(history, store):
    # Arrange — THE gate. 0 must mean "we accounted for the roster and nothing
    # is wedged", never "we produced no reports": the second is also what a
    # pass that observed nothing produces, and while both spelled 0 the timer
    # recorded ExecMainStatus=0 for every pass that had failed to look.
    rec = Recorder()
    # Act
    outcome = _run({"scitex-hub": (None, None)}, history, store, rec)
    # Assert
    assert outcome.exit_code() == 2


def test_uncapturable_pane_is_never_restarted(history, store):
    # Arrange — visible is not the same as actionable. An unread pane is no
    # evidence of a wedge, so making it loud must not make it restartable.
    rec = Recorder()
    # Act
    _run({"scitex-hub": (None, None)}, history, store, rec)
    # Assert
    assert rec.names == []


# --- absence is a value: the ROSTER is the population, not the reading -----
#
# An agent whose tmux session is GONE can never become a key in a reading built
# by enumerating live sessions, so it could not be reported as anything at all.
# The registry is the independent population that makes it visible.


def test_registered_agent_with_no_session_is_reported(roster, history, store):
    # Arrange — scitex-hub is registered; the reading contains only some other,
    # healthy agent, so nothing in it mentions scitex-hub.
    register_agents(roster, "scitex-hub")
    rec = Recorder()
    # Act
    outcome = _run({"writer": (OK, OK)}, history, store, rec)
    # Assert
    assert _verdict(outcome, "scitex-hub") == Verdict.UNOBSERVED


def test_registered_agent_with_no_session_could_not_be_determined(
    roster, history, store
):
    # Arrange
    register_agents(roster, "scitex-hub")
    rec = Recorder()
    # Act
    outcome = _run({"writer": (OK, OK)}, history, store, rec)
    # Assert
    assert outcome.exit_code() == 2


def test_registered_agent_with_no_session_is_never_restarted(roster, history, store):
    # Arrange — a missing session is fleet-reconcile's half of the fleet. This
    # pass makes it visible; it must not also act on it.
    register_agents(roster, "scitex-hub")
    rec = Recorder()
    # Act
    _run({"writer": (OK, OK)}, history, store, rec)
    # Assert
    assert rec.names == []


def test_fully_observed_healthy_roster_exits_clean(roster, history, store):
    # Arrange — the other half of the gate: 0 must stay REACHABLE, or the exit
    # code carries no information. Every registered agent has a live pane that
    # read clean, so this pass genuinely accounted for the whole roster.
    register_agents(roster, "writer")
    rec = Recorder()
    # Act
    outcome = _run({"writer": (OK, OK)}, history, store, rec)
    # Assert
    assert outcome.exit_code() == 0


# --- the roster itself is unreadable → still not a clean fleet -------------


def test_unreadable_roster_is_reported(history, store, tmp_path):
    # Arrange — the registry cannot be enumerated, so we do not know which
    # agents SHOULD have been observed. An empty roster here would silently
    # certify that nobody is missing.
    rec = Recorder()
    # Act
    outcome = _run(
        {"writer": (OK, OK)}, history, store, rec, specs_dir=tmp_path / "not-there"
    )
    # Assert
    assert [r.reason for r in outcome.reports] == ["roster-unreadable"]


def test_unreadable_roster_makes_the_pass_could_not_determine(history, store, tmp_path):
    # Arrange — every pane we did read came back clean, which is exactly the
    # shape that must NOT be allowed to look like a healthy fleet: we cannot
    # say we observed everyone without knowing who everyone is.
    rec = Recorder()
    # Act
    outcome = _run(
        {"writer": (OK, OK)}, history, store, rec, specs_dir=tmp_path / "not-there"
    )
    # Assert
    assert outcome.exit_code() == 2
