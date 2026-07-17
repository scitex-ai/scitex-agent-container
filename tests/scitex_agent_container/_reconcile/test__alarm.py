"""Tests for ``_reconcile._alarm`` — down cards + the who-watches-the-watcher beat.

No mocks: a REAL temporary scitex-todo store (``tmp_path/tasks.yaml``), the
real ``scitex_todo`` writer, and each assertion reads the card back through
the real reader. The fail-loud leg breaks the write ORGANICALLY (a read-only
parent dir) rather than injecting a raiser.

The behaviours that matter:

* an agent we could not recover gets a BLOCKING-YOU card naming it, and a
  re-run updates in place rather than duplicating;
* an agent that came back RESOLVES its card — a fixed problem stops shouting;
* the heartbeat card ticks on EVERY pass, above all the ones that found
  nothing wrong: a beacon that only appears during trouble cannot tell
  HEALTHY from DEAD, and telling those apart is its entire purpose;
* the heartbeat stays ``in_progress`` on purpose — only an OPEN card is
  watched by scitex-todo's stale-active nudge, and that nudge (delivered by
  a SEPARATE system) going off IS the alarm for a dead reconciler. Resolving
  it would silence the one thing that can report our own death;
* both rails are SIDE rails: a board failure prints loud and never raises.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scitex_agent_container._reconcile._alarm import (
    HEARTBEAT_CARD_ID,
    card_id_for,
    route_reports_to_cards,
    upsert_heartbeat,
)
from scitex_agent_container._reconcile._pass import AgentReport
from scitex_agent_container._reconcile._rule import Verdict

scitex_todo = pytest.importorskip("scitex_todo")

# ``store`` and ``unwritable`` come from conftest.py — a real temp store and
# a genuinely read-only one, shared with the _pass suites.


def _report(name: str, verdict: Verdict) -> AgentReport:
    return AgentReport(
        name=name,
        verdict=verdict,
        reason="ghost-active-row",
        detail=f"{name} died and sac could not bring it back",
    )


# --- down cards -------------------------------------------------------------


def test_over_budget_agent_gets_a_blocking_you_card(store):
    # Arrange — an agent restarting has plainly not fixed.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.OVER_BUDGET)], store=store)
    # Assert — it lands on the surface the operator already watches.
    assert [t["id"] for t in scitex_todo.list_tasks(store, blocking_me=True)] == [
        card_id_for("alpha")
    ]


def test_failed_agent_gets_a_card(store):
    # Arrange — we tried and it did not come back.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Assert
    assert scitex_todo.get_task(store, card_id_for("alpha"))["id"] == card_id_for(
        "alpha"
    )


def test_down_card_names_the_agent(store):
    # Arrange — never silent: the operator must see WHICH agent.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Assert
    assert "alpha" in scitex_todo.get_task(store, card_id_for("alpha"))["title"]


def test_second_down_run_updates_not_duplicates(store):
    # Arrange — the timer fires every 5 minutes; a card per tick would bury
    # the board in minutes.
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Act
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Assert
    assert len(scitex_todo.list_tasks(store)) == 1


def test_recovered_agent_resolves_its_card(store):
    # Arrange — alpha was down, so a card exists.
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Act — alpha is back.
    route_reports_to_cards([_report("alpha", Verdict.RESTARTED)], store=store)
    # Assert — a fixed problem stops shouting.
    assert scitex_todo.list_tasks(store, blocking_me=True) == []


def test_healthy_agent_without_prior_card_is_a_noop(store):
    # Arrange — an OK agent that was never down.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.OK)], store=store)
    # Assert — no phantom card is created just to resolve it.
    assert not Path(store).exists() or scitex_todo.list_tasks(store) == []


def test_redeath_after_recovery_reopens_the_card(store):
    # Arrange — down, then fixed (card resolved).
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    route_reports_to_cards([_report("alpha", Verdict.RESTARTED)], store=store)
    # Act — it dies AGAIN; the alarm must re-fire, not stay silent.
    route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Assert
    assert len(scitex_todo.list_tasks(store, blocking_me=True)) == 1


def test_skipped_agent_gets_no_card(store):
    # Arrange — a deliberately-stopped agent is a CORRECT state, not a
    # problem. Carding it would train the operator to ignore the board.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.SKIPPED)], store=store)
    # Assert
    assert not Path(store).exists() or scitex_todo.list_tasks(store) == []


def test_unknown_agent_gets_no_per_agent_card(store):
    # Arrange — blindness is FLEET-wide (we are in a container, or tmux is
    # wedged). One cause must not mint ~93 cards; the exit code carries it.
    # Act
    route_reports_to_cards([_report("alpha", Verdict.UNKNOWN)], store=store)
    # Assert
    assert not Path(store).exists() or scitex_todo.list_tasks(store) == []


def test_route_reports_the_carded_agent(store):
    # Arrange
    # Act
    outcome = route_reports_to_cards([_report("alpha", Verdict.FAILED)], store=store)
    # Assert
    assert outcome.carded == ("alpha",)


def test_one_bad_card_does_not_suppress_the_rest(store, unwritable):
    # Arrange — the store cannot be written at all, so BOTH agents' writes
    # fail. Neither may be silently dropped from the outcome.
    # Act
    outcome = route_reports_to_cards(
        [_report("alpha", Verdict.FAILED), _report("beta", Verdict.FAILED)],
        store=unwritable,
        err_stream=io.StringIO(),
    )
    # Assert
    assert outcome.failed == ("alpha", "beta")


def test_card_delivery_failure_is_loud(store, unwritable):
    # Arrange
    stream = io.StringIO()
    # Act
    route_reports_to_cards(
        [_report("alpha", Verdict.FAILED)], store=unwritable, err_stream=stream
    )
    # Assert
    assert "card delivery FAILED" in stream.getvalue()


def test_card_delivery_failure_does_not_raise(store, unwritable):
    # Arrange — the pass's job is restarting corpses; telling the board is
    # secondary and must never be able to take the primary down.
    # Act
    outcome = route_reports_to_cards(
        [_report("alpha", Verdict.FAILED)], store=unwritable, err_stream=io.StringIO()
    )
    # Assert
    assert outcome.carded == ()


# --- the heartbeat: who watches the watcher --------------------------------


def test_heartbeat_is_written_on_a_clean_pass(store):
    # Arrange — THE most important tick: "0 restarted, all healthy". A
    # beacon that only appears during trouble cannot prove it is alive.
    # Act
    upsert_heartbeat({"OK": 93}, mode="apply", host="host-a", store=store)
    # Assert
    assert scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["id"] == HEARTBEAT_CARD_ID


def test_heartbeat_stays_in_progress(store):
    # Arrange — only an OPEN card is watched by scitex-todo's stale-active
    # nudge, and that nudge firing IS the alarm for a dead reconciler.
    # Resolving it would silence the one thing that reports our death.
    # Act
    upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    # Assert
    assert scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["status"] == "in_progress"


def test_heartbeat_updates_in_place(store):
    # Arrange — it ticks every 5 minutes, forever. One card, not 288/day.
    upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    # Act
    upsert_heartbeat({"OK": 2}, mode="apply", host="host-a", store=store)
    # Assert
    assert len(scitex_todo.list_tasks(store)) == 1


def test_heartbeat_records_the_counts(store):
    # Arrange — the note is what the operator reads to see the last pass.
    # Act
    upsert_heartbeat(
        {"OK": 90, "RESTARTED": 3}, mode="apply", host="host-a", store=store
    )
    # Assert
    assert "RESTARTED" in scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["note"]


def test_heartbeat_records_the_mode(store):
    # Arrange — a hand-run dry-run also refreshes this card, so a stale
    # TIMER must stay visible even when the card itself looks fresh.
    # Act
    upsert_heartbeat({"OK": 1}, mode="dry-run", host="host-a", store=store)
    # Assert
    assert "dry-run" in scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["note"]


def test_heartbeat_refreshes_its_timestamp(store):
    # Arrange — a stale last_activity is exactly what the board's nudge
    # keys off, so a live tick MUST move it. A fixed clock makes the
    # refresh observable without sleeping.
    upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    later = datetime(2031, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # stx-allow: STX-NL001
    # Act
    upsert_heartbeat({"OK": 2}, mode="apply", host="host-a", store=store, now=later)
    # Assert
    assert (
        scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["last_activity"]
        == "2031-07-16T12:00:00Z"
    )


def test_heartbeat_stamp_matches_the_stores_own_format(store):
    # Arrange — `last_activity` is what the board's stale-active nudge reads
    # to notice the reconciler went quiet, and that nudge IS the alarm for a
    # dead reconciler. A stamp in OUR format rather than the store's risks
    # being unparseable there — silently disarming the one rail that can
    # report our own death. The store's own helper trims microseconds and
    # uses `Z` (not `+00:00`) so the string round-trips through YAML.
    #
    # The stamp WE write is the one on the UPDATE path — which is every tick
    # after the very first (the create is stamped by the store itself). So
    # create first, then drive the refresh with a microsecond-bearing clock.
    upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    now = datetime(  # stx-allow: STX-NL001
        2031, 7, 16, 12, 0, 0, 123_456, tzinfo=timezone.utc
    )
    # Act
    upsert_heartbeat({"OK": 2}, mode="apply", host="host-a", store=store, now=now)
    # Assert — microseconds trimmed, `Z` suffix, exactly like the store's own.
    assert (
        scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["last_activity"]
        == "2031-07-16T12:00:00Z"
    )


def test_heartbeat_says_what_its_staleness_means(store):
    # Arrange — the card must teach the reader what to conclude from it,
    # because its whole value is realised when nobody is expecting it.
    # Act
    upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    # Assert
    assert "NOT RUNNING" in scitex_todo.get_task(store, HEARTBEAT_CARD_ID)["note"]


def test_heartbeat_reports_success(store):
    # Arrange
    # Act
    written = upsert_heartbeat({"OK": 1}, mode="apply", host="host-a", store=store)
    # Assert
    assert written


def test_heartbeat_failure_does_not_raise(store, unwritable):
    # Arrange — a SIDE rail: telling the board we are alive must never
    # crash the pass that restarts corpses.
    # Act
    written = upsert_heartbeat(
        {"OK": 1}, mode="apply", store=unwritable, err_stream=io.StringIO()
    )
    # Assert
    assert not written


def test_heartbeat_failure_is_loud(store, unwritable):
    # Arrange — if the beacon dies quietly, nobody learns the watcher is
    # unwatched.
    stream = io.StringIO()
    # Act
    upsert_heartbeat({"OK": 1}, mode="apply", store=unwritable, err_stream=stream)
    # Assert
    assert "HEARTBEAT card delivery FAILED" in stream.getvalue()
