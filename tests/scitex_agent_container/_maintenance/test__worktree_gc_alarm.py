"""Tests for ``_maintenance._worktree_gc_alarm`` — over-cap → SEEN card.

PA-306: no ``unittest.mock``. Real :class:`RepoGcResult` values and a REAL
temporary scitex-todo store (``tmp_path/tasks.yaml``); the routing calls
the real ``scitex_todo`` writer and each test reads the card back through
the real reader.

The behaviours that matter:

* over cap → an upserted BLOCKING-YOU card naming the repo and count,
* the card carries the kept-reasons BREAKDOWN (the actionable part),
* a second over-cap run UPDATES in place (never duplicates),
* back under cap → the card is RESOLVED (a fixed repo stops shouting),
* UNREADABLE is carded too but labelled UNKNOWN — never rendered clean,
* re-sprawl after a clear REOPENS the card,
* a board-write failure NEVER crashes the GC (delivery is a side rail).

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from scitex_agent_container._maintenance._worktree_gc_alarm import (
    card_id_for,
    route_gc_to_cards,
)
from scitex_agent_container._maintenance._worktree_gc_model import (
    KEEP_DIRTY,
    KEEP_UNMERGED,
    RepoGcResult,
    WorktreeVerdict,
)

scitex_todo = pytest.importorskip("scitex_todo")


def _kept(path: str, *reasons: str) -> WorktreeVerdict:
    return WorktreeVerdict(path=path, branch="feat/x", keep_reasons=tuple(reasons))


def _over_cap(repo: str = "/proj/sprawly", cap: int = 1) -> RepoGcResult:
    """A real result: two survivors against a cap of one."""
    return RepoGcResult(
        repo=repo,
        applied=True,
        cap=cap,
        verdicts=(
            _kept("/wt/a", KEEP_DIRTY),
            _kept("/wt/b", KEEP_UNMERGED),
        ),
    )


def _under_cap(repo: str = "/proj/sprawly", cap: int = 20) -> RepoGcResult:
    return RepoGcResult(
        repo=repo, applied=True, cap=cap, verdicts=(_kept("/wt/a", KEEP_DIRTY),)
    )


def _unreadable(repo: str = "/proj/broken") -> RepoGcResult:
    return RepoGcResult(repo=repo, applied=True, error="not a git repository")


@pytest.fixture
def store(tmp_path: Path) -> str:
    """A real (initially absent) scitex-todo store path — no mocks."""
    return str(tmp_path / "tasks.yaml")


def test_over_cap_upserts_a_blocking_you_card(store):
    # Arrange — a repo with more survivors than its cap allows.
    # Act
    route_gc_to_cards([_over_cap()], store=store)
    # Assert — it lands on the board's BLOCKING-YOU seen surface.
    blocking = scitex_todo.list_tasks(store, blocking_me=True)
    assert [t["id"] for t in blocking] == [card_id_for("/proj/sprawly")]


def test_over_cap_card_names_the_repo(store):
    # Arrange
    # Act
    route_gc_to_cards([_over_cap()], store=store)
    # Assert — never silent: the operator must see WHICH repo.
    card = scitex_todo.get_task(store, card_id_for("/proj/sprawly"))
    assert "sprawly" in card["title"]


def test_over_cap_card_names_the_count(store):
    # Arrange — two survivors against a cap of one.
    # Act
    route_gc_to_cards([_over_cap()], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("/proj/sprawly"))
    assert "2 worktrees" in card["title"]


def test_over_cap_card_carries_the_reason_breakdown(store):
    # Arrange — "2 kept" is a number; "1 dirty, 1 unmerged" is an
    # instruction. The breakdown is the card's entire value.
    # Act
    route_gc_to_cards([_over_cap()], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("/proj/sprawly"))
    assert "1 dirty" in card["note"] and "1 unmerged" in card["note"]


def test_second_over_cap_run_updates_not_duplicates(store):
    # Arrange — first run creates the card.
    route_gc_to_cards([_over_cap()], store=store)
    # Act — a nightly timer must UPDATE in place, not tile the board.
    route_gc_to_cards([_over_cap()], store=store)
    # Assert
    assert len(scitex_todo.list_tasks(store)) == 1


def test_back_under_cap_resolves_the_card(store):
    # Arrange — the repo sprawled, so a card exists.
    route_gc_to_cards([_over_cap()], store=store)
    # Act — the operator cleaned it up.
    route_gc_to_cards([_under_cap()], store=store)
    # Assert — a fixed repo stops shouting (off the BLOCKING-YOU view).
    assert scitex_todo.list_tasks(store, blocking_me=True) == []


def test_back_under_cap_marks_the_card_done(store):
    # Arrange
    route_gc_to_cards([_over_cap()], store=store)
    # Act
    route_gc_to_cards([_under_cap()], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("/proj/sprawly"))
    assert card["status"] == "done"


def test_healthy_repo_without_prior_card_is_a_noop(store):
    # Arrange — a repo that was never over cap.
    # Act
    route_gc_to_cards([_under_cap()], store=store)
    # Assert — no phantom card is created just to resolve it.
    assert not Path(store).exists() or scitex_todo.list_tasks(store) == []


def test_unreadable_repo_gets_a_card(store):
    # Arrange — UNKNOWN must be surfaced, not swallowed.
    # Act
    route_gc_to_cards([_unreadable()], store=store)
    # Assert
    card_id = card_id_for("/proj/broken")
    assert scitex_todo.get_task(store, card_id)["id"] == card_id


def test_unreadable_card_is_labelled_unknown(store):
    # Arrange — "I could not look" must never read as "I looked, it's fine".
    # Act
    route_gc_to_cards([_unreadable()], store=store)
    # Assert
    card = scitex_todo.get_task(store, card_id_for("/proj/broken"))
    assert "UNKNOWN" in card["title"]


def test_route_buckets_over_cap_and_unknown_separately(store):
    # Arrange — a sprawling repo AND an unreadable one in one run.
    # Act
    outcome = route_gc_to_cards([_over_cap(), _unreadable()], store=store)
    # Assert — three-state honest: distinct buckets, never merged.
    assert (outcome.exceeded, outcome.unreadable) == (
        ("/proj/sprawly",),
        ("/proj/broken",),
    )


def test_route_reports_the_cleared_repo(store):
    # Arrange — sprawl first so there is a card to clear.
    route_gc_to_cards([_over_cap()], store=store)
    # Act
    outcome = route_gc_to_cards([_under_cap()], store=store)
    # Assert
    assert outcome.cleared == ("/proj/sprawly",)


def test_resprawl_after_clear_reopens_the_card(store):
    # Arrange — over cap, then cleaned (card resolved).
    route_gc_to_cards([_over_cap()], store=store)
    route_gc_to_cards([_under_cap()], store=store)
    # Act — it sprawls AGAIN; the alarm must re-fire, not stay silent.
    route_gc_to_cards([_over_cap()], store=store)
    # Assert
    assert len(scitex_todo.list_tasks(store, blocking_me=True)) == 1


def test_board_write_failure_does_not_raise(tmp_path):
    # Arrange — an unwritable store path (a directory where the YAML file
    # should be). Card delivery is a SIDE rail: it must never crash the GC
    # pass that feeds it.
    unwritable = tmp_path / "tasks.yaml"
    unwritable.mkdir()
    # Act
    outcome = route_gc_to_cards(
        [_over_cap()], store=str(unwritable), err_stream=io.StringIO()
    )
    # Assert — recorded as failed, not raised.
    assert outcome.failed == ("/proj/sprawly",)


def test_board_write_failure_is_printed_loudly(tmp_path):
    # Arrange — a failure nobody hears is the anti-pattern this whole rail
    # exists to fix, so the side rail still SHOUTS on stderr.
    unwritable = tmp_path / "tasks.yaml"
    unwritable.mkdir()
    stream = io.StringIO()
    # Act
    route_gc_to_cards([_over_cap()], store=str(unwritable), err_stream=stream)
    # Assert
    assert "FAILED" in stream.getvalue()


def test_card_id_is_stable_for_a_repo_path():
    # Arrange — idempotency is BY ID: an unstable id tiles the board.
    # Act
    card_id = card_id_for("/home/user/proj/scitex-todo")
    # Assert
    assert card_id == "worktree-sprawl-scitex-todo"
