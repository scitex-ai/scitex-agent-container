"""Tests for ``_hostsync._alarm`` — drift verdict → SEEN scitex-todo card.

PA-306: no ``unittest.mock``. Real :class:`SyncResult` / :class:`PeerSyncReport`
objects and a REAL temporary scitex-todo store (``tmp_path/tasks.yaml``);
the routing calls the real ``scitex_todo`` writer and each test reads the
card back through the real ``scitex_todo`` reader.

The behaviours that matter:

* drift → an upserted BLOCKING-YOU card (``status=blocked`` /
  ``blocker=operator-decision``) NAMING the peer,
* a second drift run UPDATES in place (never duplicates),
* a clean run RESOLVES the peer's card (a fixed drift stops shouting),
* UNKNOWN is carded too but labelled UNKNOWN — never rendered as clean,
* re-drift after a clear REOPENS the card.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._hostsync import route_reports_to_cards
from scitex_agent_container._hostsync._alarm import card_id_for
from scitex_agent_container._hostsync._model import GraphState, PeerSyncReport
from scitex_agent_container._hostsync._sync import Outcome, SyncResult

scitex_todo = pytest.importorskip("scitex_todo")


def _behind(peer: str = "spartan", behind: int = 4) -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.BEHIND,
        head="aaa111",
        target="origin/develop",
        target_sha="bbb222",
        behind=behind,
        repo="/checkout",
        module="/checkout/src/scitex_agent_container/__init__.py",
        symbol="['agent_name']",
    )
    return SyncResult(peer=peer, outcome=Outcome.DRIFTED, before=report)


def _current(peer: str = "spartan") -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.CURRENT,
        head="aaa111",
        target="origin/develop",
        target_sha="aaa111",
        repo="/checkout",
        module="/checkout/src/scitex_agent_container/__init__.py",
        symbol="['agent_name']",
    )
    return SyncResult(peer=peer, outcome=Outcome.CURRENT, before=report)


def _unreachable(peer: str = "nas") -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.UNREACHABLE,
        detail="ssh: connect: refused",
    )
    return SyncResult(peer=peer, outcome=Outcome.UNDETERMINED, before=report)


@pytest.fixture
def store(tmp_path: Path) -> str:
    """A real (initially absent) scitex-todo store path — no mocks."""
    return str(tmp_path / "tasks.yaml")


def test_drift_upserts_a_blocking_you_card(store):
    # Arrange — one peer 4 commits behind the centre.
    # Act
    route_reports_to_cards([_behind("spartan")], store=store)
    # Assert — it lands on the board's BLOCKING-YOU seen surface.
    blocking = scitex_todo.list_tasks(store, blocking_me=True)
    assert [t["id"] for t in blocking] == [card_id_for("spartan")]


def test_drift_card_names_the_peer(store):
    # Arrange
    # Act
    route_reports_to_cards([_behind("spartan")], store=store)
    # Assert — never silent: the operator must see WHICH peer.
    card = scitex_todo.get_task(store, card_id_for("spartan"))
    assert "spartan" in card["title"]


def test_drift_card_names_the_concrete_drift(store):
    # Arrange — behind is the concrete drift class; the card must say so.
    # Act
    route_reports_to_cards([_behind("spartan", behind=4)], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("spartan"))
    assert "behind" in card["note"].lower()


def test_second_drift_run_updates_not_duplicates(store):
    # Arrange — first run creates the card.
    route_reports_to_cards([_behind("spartan")], store=store)
    # Act — a second drift run must UPDATE in place, not add a twin.
    route_reports_to_cards([_behind("spartan")], store=store)
    # Assert — exactly one card exists for the peer.
    assert len(scitex_todo.list_tasks(store)) == 1


def test_clean_run_resolves_the_drift_card(store):
    # Arrange — the peer drifted, so a card exists.
    route_reports_to_cards([_behind("spartan")], store=store)
    # Act — the peer is now current with the centre.
    route_reports_to_cards([_current("spartan")], store=store)
    # Assert — a fixed drift stops shouting (off the BLOCKING-YOU view).
    assert scitex_todo.list_tasks(store, blocking_me=True) == []


def test_clean_run_marks_the_card_done(store):
    # Arrange
    route_reports_to_cards([_behind("spartan")], store=store)
    # Act
    route_reports_to_cards([_current("spartan")], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("spartan"))
    assert card["status"] == "done"


def test_clean_peer_without_prior_card_is_a_noop(store):
    # Arrange — a peer that is current and was NEVER drifted.
    # Act
    route_reports_to_cards([_current("spartan")], store=store)
    # Assert — no phantom card is created just to resolve it.
    assert not Path(store).exists() or scitex_todo.list_tasks(store) == []


def test_undetermined_peer_gets_a_card(store):
    # Arrange — an unreachable peer. UNKNOWN must be surfaced, not swallowed.
    # Act
    route_reports_to_cards([_unreachable("nas")], store=store)
    # Assert
    assert scitex_todo.get_task(store, card_id_for("nas"))["id"] == card_id_for("nas")


def test_undetermined_card_is_labelled_unknown(store):
    # Arrange — "I could not look" must never read as "I looked, it's fine".
    # Act
    route_reports_to_cards([_unreachable("nas")], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("nas"))
    assert "UNKNOWN" in card["title"]


def test_route_buckets_drift_and_unknown_separately(store):
    # Arrange — a drifted peer AND an unreachable peer in one run.
    # Act
    outcome = route_reports_to_cards(
        [_behind("spartan"), _unreachable("nas")], store=store
    )
    # Assert — drift and unknown are distinct buckets (three-state honest).
    assert (outcome.drifted, outcome.undetermined) == (("spartan",), ("nas",))


def test_route_reports_the_cleared_peer(store):
    # Arrange — drift first so there is a card to clear.
    route_reports_to_cards([_behind("spartan")], store=store)
    # Act
    outcome = route_reports_to_cards([_current("spartan")], store=store)
    # Assert
    assert outcome.cleared == ("spartan",)


def test_redrift_after_clear_reopens_the_card(store):
    # Arrange — drift, then fixed (card resolved).
    route_reports_to_cards([_behind("spartan")], store=store)
    route_reports_to_cards([_current("spartan")], store=store)
    # Act — the peer drifts AGAIN; the alarm must re-fire, not stay silent.
    route_reports_to_cards([_behind("spartan")], store=store)
    # Assert — the card is back on the BLOCKING-YOU view.
    assert len(scitex_todo.list_tasks(store, blocking_me=True)) == 1
