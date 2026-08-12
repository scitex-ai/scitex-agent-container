#!/usr/bin/env python3
"""Ten agents that passed every check and could not move, because of one `== 1`.

The guard this module replaces read ``if len(plan.files) == 1``. Measured on
ywata-note-win on 2026-08-12, not one of the ten agents left to relocate has
exactly one transcript — they hold 3, 4, 4, 5, 3, 2, 3, 2, 4 and 4 — so every one
of them reported ``GO`` from preflight and then aborted at TARGET_STANDBY with the
agent already stopped, the transcript already copied, and no marker written.

The tests here are the two halves of the fix: a session is CHOSEN from real
candidates by stated evidence, and an unresolvable one refuses NAMING what it saw
rather than picking. The counts and file names are the real ones where the real
ones are known.

Pure values in, a choice out. Nothing is mocked because there is nothing to mock.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_session_choice import (
    CODE_AMBIGUOUS,
    CODE_CHOSEN,
    CODE_MARKER_NOT_CARRIED,
    CODE_NO_CANDIDATES,
    CODE_UNKNOWN,
    SessionChoice,
    choose_session,
    session_id_for,
)

# Three transcripts, as scitex-clew actually held them on the night this failed.
CLEW = ("aaa1.jsonl", "bbb2.jsonl", "ccc3.jsonl")
CLEW_TIMES = {"aaa1.jsonl": 1000, "bbb2.jsonl": 3000, "ccc3.jsonl": 2000}


# --------------------------------------------------------------------------
# The marker is preferred, because it REPORTS rather than infers
# --------------------------------------------------------------------------


def test_the_marked_session_is_chosen_from_several_transcripts() -> None:
    # Arrange: THE case that could not complete. Three carried transcripts and a
    # runtime that already knows which one it is having.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="aaa1", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.session == "aaa1"


def test_the_marker_beats_the_newest_file() -> None:
    # Arrange: the ordering matters and must be falsifiable. The marker names the
    # OLDEST here, so a test that only had one candidate could not tell the two
    # rules apart.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="aaa1", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.session != session_id_for("bbb2.jsonl")


def test_a_chosen_session_says_what_chose_it() -> None:
    # Arrange: "the marker said so" and "it looked newest" are different levels
    # of evidence, and a reader must be able to tell which one they are trusting.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="aaa1", mtimes=CLEW_TIMES
    )
    # Assert
    assert "marker" in choice.chosen_by


def test_a_choice_reports_every_candidate_it_saw() -> None:
    # Arrange: a choice among four that prints only the winner cannot be
    # reviewed, and reviewing it is the whole point of choosing deliberately.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="aaa1", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.candidates == CLEW


# --------------------------------------------------------------------------
# No marker: the most recently modified travels
# --------------------------------------------------------------------------


def test_the_newest_transcript_is_chosen_when_there_is_no_marker() -> None:
    # Arrange: the second rule. The live conversation is the one being written,
    # so mtime orders them — but only once the marker is known to be absent.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.session == "bbb2"


def test_the_newest_choice_says_it_reasoned_from_mtime() -> None:
    # Arrange: weaker evidence than the marker, and it must say so rather than
    # presenting itself as the same answer.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="", mtimes=CLEW_TIMES
    )
    # Assert
    assert "recently modified" in choice.chosen_by


def test_a_single_transcript_still_resolves() -> None:
    # Arrange: the case the old guard handled, which must not regress. One file
    # is the conversation whether or not a marker was read.
    # Act
    choice = choose_session(agent="a", carried=("only.jsonl",), marker="")
    # Assert
    assert choice.session == "only"


def test_a_single_transcript_resolves_even_with_no_marker_read() -> None:
    # Arrange: an unread marker cannot contradict a set of one — the only file
    # that travels is the only conversation that can be resumed.
    # Act
    choice = choose_session(agent="a", carried=("only.jsonl",), marker=None)
    # Assert
    assert choice.code == CODE_CHOSEN


# --------------------------------------------------------------------------
# The refusals — each names what it saw
# --------------------------------------------------------------------------


def test_a_marker_naming_a_file_that_was_not_carried_refuses() -> None:
    # Arrange: the runtime and the transport disagree about which conversation
    # this agent is having. Picking either one silently starts the target on an
    # undefined session, which no byte count catches.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="zzz9", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.session is None


def test_that_refusal_carries_its_own_code() -> None:
    # Arrange: callers branch on the code, never on prose, and this one calls for
    # a different action than "go and measure it".
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="zzz9", mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.code == CODE_MARKER_NOT_CARRIED


def test_that_refusal_names_the_candidates_it_saw() -> None:
    # Arrange: THE operator's requirement. A refusal that says only "could not
    # choose" turns a one-command fix into an investigation across two hosts.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="zzz9", mtimes=CLEW_TIMES
    )
    # Assert
    assert all(name in choice.reason for name in CLEW)


def test_that_refusal_also_names_the_session_the_marker_wanted() -> None:
    # Arrange: half the disagreement is the marker's side of it, and the fix is
    # usually to that side.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker="zzz9", mtimes=CLEW_TIMES
    )
    # Assert
    assert "zzz9" in choice.reason


def test_zero_transcripts_refuses() -> None:
    # Arrange: existing behaviour that must not regress. An agent started with no
    # session resumes nothing — it boots, reports healthy, and has no memory.
    # Act
    choice = choose_session(agent="a", carried=(), marker="x")
    # Assert
    assert choice.code == CODE_NO_CANDIDATES


def test_an_unread_marker_with_several_candidates_is_unknown_not_newest() -> None:
    # Arrange: an unread marker is NOT an absent one. It may well name a file
    # other than the newest, which is exactly the case where guessing is wrong.
    # Act
    choice = choose_session(
        agent="scitex-clew", carried=CLEW, marker=None, mtimes=CLEW_TIMES
    )
    # Assert
    assert choice.code == CODE_UNKNOWN


def test_an_unmeasured_mtime_refuses_rather_than_sorting_around_it() -> None:
    # Arrange: a missing mtime read as 0 would lose every comparison silently and
    # the file would simply never be chosen. Refusing names it instead.
    # Act
    choice = choose_session(
        agent="a",
        carried=("a.jsonl", "b.jsonl"),
        marker="",
        mtimes={"a.jsonl": 5, "b.jsonl": None},
    )
    # Assert
    assert choice.code == CODE_UNKNOWN


def test_two_transcripts_sharing_the_newest_mtime_refuse() -> None:
    # Arrange: "the newest" is not an answer when two are equally newest. Picking
    # either would be the silent guess this module exists to remove.
    # Act
    choice = choose_session(
        agent="a",
        carried=("a.jsonl", "b.jsonl"),
        marker="",
        mtimes={"a.jsonl": 9, "b.jsonl": 9},
    )
    # Assert
    assert choice.code == CODE_AMBIGUOUS


def test_the_tie_refusal_names_both_tied_files() -> None:
    # Arrange: the operator resolves this by seeding the marker, and cannot do
    # that without knowing which two are in contention.
    # Act
    choice = choose_session(
        agent="a",
        carried=("a.jsonl", "b.jsonl"),
        marker="",
        mtimes={"a.jsonl": 9, "b.jsonl": 9},
    )
    # Assert
    assert "a.jsonl" in choice.reason and "b.jsonl" in choice.reason


def test_every_refusal_says_what_to_do_next() -> None:
    # Arrange: an unresolved session stops a relocation and the operator is the
    # one who resolves it, so the invariant lives in the type.
    # Act
    build = lambda: SessionChoice(  # noqa: E731
        session=None, code=CODE_NO_CANDIDATES, reason="nothing"
    )
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_chosen_session_must_state_its_evidence() -> None:
    # Arrange: the other half of the same invariant — a choice with no stated
    # basis is indistinguishable from a guess.
    # Act
    build = lambda: SessionChoice(  # noqa: E731
        session="x", code=CODE_CHOSEN, reason="because"
    )
    # Assert
    with pytest.raises(ValueError):
        build()
