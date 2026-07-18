"""The board rail: escalate the unhealable, resolve the recovered.

The card is the alternative to an infinite bounce — it must appear when an
agent is over the hourly cap, and it must go away on its own the moment the
agent is no longer login-expired, or the operator learns to scroll past a
board full of stale alarms. Real scitex-todo store, no mocks.
"""

from __future__ import annotations

from scitex_agent_container._authheal._alarm import (
    card_id_for,
    route_reports_to_cards,
)
from scitex_agent_container._authheal._pass import AgentReport
from scitex_agent_container._reconcile._rule import Verdict


def _report(name: str, verdict: Verdict) -> AgentReport:
    return AgentReport(name=name, verdict=verdict, reason="t", detail="detail")


def test_over_budget_report_creates_a_card(store):
    # Arrange — an agent the restarter has given up on.
    from scitex_todo import get_task

    reports = [_report("hpc", Verdict.OVER_BUDGET)]
    # Act
    route_reports_to_cards(reports, store=store)
    # Assert — the card exists and is a live BLOCKING-YOU alarm.
    assert get_task(store, card_id_for("hpc"))["status"] == "blocked"


def test_restarted_report_resolves_a_prior_card(store):
    # Arrange — first over budget (carded), then successfully restarted.
    from scitex_todo import get_task

    route_reports_to_cards([_report("hpc", Verdict.OVER_BUDGET)], store=store)
    # Act
    route_reports_to_cards([_report("hpc", Verdict.RESTARTED)], store=store)
    # Assert
    assert get_task(store, card_id_for("hpc"))["status"] == "done"


def test_recovered_agent_no_longer_in_reports_is_resolved(store):
    # Arrange — carded, then the agent recovers on its own (operator logged
    # in) so it is not login-expired anymore and never appears in a later
    # pass's reports at all.
    from scitex_todo import get_task

    route_reports_to_cards([_report("hpc", Verdict.OVER_BUDGET)], store=store)
    # Act — a subsequent pass finds nothing wrong.
    route_reports_to_cards([], store=store)
    # Assert — the stale card is cleared without a human.
    assert get_task(store, card_id_for("hpc"))["status"] == "done"


def test_over_budget_card_is_reported_as_carded(store):
    # Arrange
    reports = [_report("hpc", Verdict.OVER_BUDGET)]
    # Act
    outcome = route_reports_to_cards(reports, store=store)
    # Assert
    assert outcome.carded == ("hpc",)
