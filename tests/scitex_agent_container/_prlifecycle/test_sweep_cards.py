"""The card-per-open-PR sweep, driven against a REAL store and a RECORDED gh.

No mocks: the store is a real on-disk scitex-todo YAML that the pass writes and
the test reads back through the real API, and the ``gh`` payload is an actual
captured response (see ``conftest``). What is asserted here is bookkeeping
behaviour — idempotency, completion-on-close, and the facts a card must carry.

The tri-state lives in ``test_tristate.py``; this file assumes a readable fetch.
"""

from __future__ import annotations

import scitex_todo

from scitex_agent_container._prlifecycle import card_id_for, fetch_open_prs, sync_cards
from scitex_agent_container._prlifecycle._cards import HEARTBEAT_CARD_ID

from .conftest import RECORDED_REPO, gh_returning


def _fetch(recorded_gh):
    def fetch(repo):
        return fetch_open_prs(repo, run=recorded_gh)

    return fetch


def _first_pr(recorded_rows):
    return sorted(recorded_rows, key=lambda r: r["number"])[0]


def test_every_open_pr_gets_a_card(store, recorded_gh, recorded_rows) -> None:
    # Arrange — the whole point: a PR with no card is invisible to the fleet,
    # which is how 35 open PRs accumulated until 31 were force-closed by hand.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    carded = [
        r
        for r in recorded_rows
        if scitex_todo.get_task(store, card_id_for(RECORDED_REPO, r["number"]))
    ]
    # Assert
    assert len(carded) == len(recorded_rows)


def test_the_card_id_is_derived_from_the_pr_number(store, recorded_gh, recorded_rows):
    # Arrange — idempotency is BY ID, so the id must be a pure function of
    # (repo, number) and nothing else (not the title, which changes).
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert card["id"] == card_id_for(RECORDED_REPO, pr["number"])


def test_a_second_pass_does_not_duplicate_cards(store, recorded_gh, recorded_rows):
    # Arrange — a timer runs this every 30 minutes forever. Duplication here
    # would bury the board within a day.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act — every pr-card for this repo, counted.
    rows = scitex_todo.list_tasks(store, id_prefix="sac-pr-")
    pr_cards = [r for r in rows if r["id"] != HEARTBEAT_CARD_ID]
    # Assert
    assert len(pr_cards) == len(recorded_rows)


def test_the_card_carries_the_author(store, recorded_gh, recorded_rows) -> None:
    # Arrange — the brief's required facts, one test each so a failure names
    # exactly which fact went missing.
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert pr["author"]["login"] in card["note"]


def test_the_card_carries_the_title(store, recorded_gh, recorded_rows) -> None:
    # Arrange
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert pr["title"].strip() in card["note"]


def test_the_card_carries_the_draft_state(store, recorded_gh, recorded_rows) -> None:
    # Arrange
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert f"draft:    {'yes' if pr['isDraft'] else 'no'}" in card["note"]


def test_the_card_carries_the_ci_status(store, recorded_gh, recorded_rows) -> None:
    # Arrange — derived from the REAL statusCheckRollup, which is why the
    # recorded fixture matters: an IN_PROGRESS run carries an empty
    # `conclusion`, so conclusion alone cannot tell pending from success.
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert "ci:       " in card["note"]


def test_the_card_carries_the_age_in_days(store, recorded_gh, recorded_rows) -> None:
    # Arrange
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert "day(s) ago" in card["note"]


def test_the_card_is_left_OPEN_so_the_nudge_rail_can_see_it(
    store, recorded_gh, recorded_rows
) -> None:
    # Arrange — LOAD-BEARING. scitex-todo's stale-active sweep only nudges on
    # OPEN cards, and that nudge is the entire reason sac writes these. A card
    # created `done` would be perfect bookkeeping that nudges nobody.
    pr = _first_pr(recorded_rows)
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, pr["number"]))
    # Assert
    assert card["status"] == "in_progress"


def test_a_pr_that_closed_has_its_card_completed(store, recorded_gh) -> None:
    # Arrange — first pass sees the recorded backlog; the second sees an EMPTY
    # (but genuinely READ) list, i.e. everything merged or closed.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))

    def empty(repo):
        return fetch_open_prs(repo, run=gh_returning("[]"))

    # Act
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=empty)
    rows = scitex_todo.list_tasks(store, id_prefix="sac-pr-")
    open_pr_cards = [
        r for r in rows if r["id"] != HEARTBEAT_CARD_ID and r["status"] != "done"
    ]
    # Assert
    assert open_pr_cards == []


def test_a_dry_run_writes_no_pr_card(store, recorded_gh) -> None:
    # Arrange — --check must mutate nothing but the heartbeat.
    sync_cards([RECORDED_REPO], apply=False, store=store, fetch=_fetch(recorded_gh))
    # Act
    rows = scitex_todo.list_tasks(store, id_prefix="sac-pr-")
    pr_cards = [r for r in rows if r["id"] != HEARTBEAT_CARD_ID]
    # Assert
    assert pr_cards == []


def test_the_sweep_refreshes_its_own_heartbeat(store, recorded_gh) -> None:
    # Arrange — who watches the watcher. If this sweep stops ticking, its
    # heartbeat card goes stale and scitex-todo's nudge shouts — one system's
    # silence becomes another's alarm.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    card = scitex_todo.get_task(store, HEARTBEAT_CARD_ID)
    # Assert
    assert card["status"] == "in_progress"


def test_an_absent_store_reads_as_no_cards_not_as_unreadable(store) -> None:
    # Arrange — the FIRST RUN. The store file does not exist yet, so there are
    # demonstrably no cards: that is a real answer, not a failure to look.
    # Collapsing it into "unreadable" would print a scary BOARD-could-not-be-
    # read on every pass of a fresh install and train the operator to ignore
    # the one message that matters when it IS real.
    from scitex_agent_container._prlifecycle._cards import open_card_numbers

    # Act
    board = open_card_numbers(RECORDED_REPO, store=store)
    # Assert — an empty dict (read, nothing there), NOT None (could not read).
    assert board == {}


def test_a_first_pass_on_a_fresh_store_does_not_warn_about_the_board(
    store, recorded_gh, capsys
) -> None:
    # Arrange — the operator-visible half of the same point.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_fetch(recorded_gh))
    # Act
    captured = capsys.readouterr()
    # Assert
    assert "BOARD could not be read" not in captured.err


def test_the_heartbeat_records_a_blind_pass_as_blind(store) -> None:
    # Arrange — a heartbeat that says "I ran" without saying "I was blind"
    # would let a board reader conclude the sweep is healthy. The unknown must
    # reach the card too, not only the exit code.
    from .conftest import gh_failing

    def blind(repo):
        return fetch_open_prs(
            repo, run=gh_failing(returncode=4, stderr="gh auth login")
        )

    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=blind)
    # Act
    card = scitex_todo.get_task(store, HEARTBEAT_CARD_ID)
    # Assert
    assert "unauthenticated" in card["note"]
