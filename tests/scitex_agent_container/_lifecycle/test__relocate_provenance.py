#!/usr/bin/env python3
"""The operator's actual requirement, and the half of it that is usually missing.

    「エージェントがどこから来たのかっていうのが分かって、そこを調査することが
    できるならば、全く問題ない」

    — if the agent can tell where it came from and go and investigate there,
      there is no problem at all.  (operator, 2026-08-12)

He was answering a question about how to MERGE a `memory/` directory that may
have diverged on two hosts, and his answer was that the merge is 枝葉 — a leaf —
and provenance is the trunk. So the tests that matter here are: does the record
name the source host and the source path, and does it name what did NOT travel.

That second one is the half that goes missing. `memory/` was stranded on
2026-08-11 with the refusal correctly logged — as one line in a run log nobody
keeps. These tests pin it into a file on the target instead.

Pure strings. Nothing is mocked because there is nothing to mock.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_provenance import (
    PROVENANCE_FILENAME,
    render_provenance,
)

WHEN = 1786492800.0
REFUSED = (
    ("b68520e1-78fb", "the allowlist carries transcripts and memory/; this is neither"),
    (".credentials.json", "a credential is never carried — the target re-issues its own"),
)


def _record(**over) -> str:
    kwargs = dict(
        agent="scitex-agent-container",
        from_host="ywata-note-win",
        source_dir="/home/ywatanabe/.claude/projects/-home-ywatanabe-proj-sac",
        to_host="scitex-compute-04",
        target_dir="/home/agent/.claude/projects/-home-ywatanabe-proj-sac",
        when=WHEN,
        session="b68520e1-78fb-404f-a84d-b78cf7cf6e31",
        transcripts=(("b68520e1.jsonl", 108412278, 58325),),
        directories=("memory",),
        refused=REFUSED,
    )
    kwargs.update(over)
    return render_provenance(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Provenance: where it came from
# --------------------------------------------------------------------------


def test_the_record_names_the_host_it_came_from() -> None:
    # Arrange: THE requirement, in the operator's own words. Everything else in
    # this module is secondary to an agent being able to answer "where was I".
    # Act
    text = _record()
    # Assert
    assert "ywata-note-win" in text


def test_the_record_names_the_absolute_source_path() -> None:
    # Arrange: a host name alone sends someone hunting. "Go and investigate
    # there" needs a path they can cd to.
    # Act
    text = _record()
    # Assert
    assert "/home/ywatanabe/.claude/projects/-home-ywatanabe-proj-sac" in text


def test_the_record_carries_the_raw_unix_time() -> None:
    # Arrange: the operator ruled clock skew 「そんなシビアじゃない」 and plain
    # unix time enough. The raw number is printed so anyone who does care about
    # skew can see exactly what this host believed the time was.
    # Act
    text = _record()
    # Assert
    assert "1786492800" in text


def test_the_record_says_the_source_still_holds_everything() -> None:
    # Arrange: the sentence that makes "go and look there" true. Nothing is ever
    # deleted on the source, and the reader must not have to know that already.
    # Act
    text = _record()
    # Assert
    assert "NOTHING WAS DELETED ON THE SOURCE" in text


def test_the_record_names_the_session_that_was_resumed() -> None:
    # Arrange: an agent reading this is one that may be asking which of several
    # conversations it is now in.
    # Act
    text = _record()
    # Assert
    assert "b68520e1-78fb-404f-a84d-b78cf7cf6e31" in text


# --------------------------------------------------------------------------
# What did NOT travel — the half that goes missing
# --------------------------------------------------------------------------


def test_the_record_names_what_was_not_carried() -> None:
    # Arrange: THE 2026-08-11 shape. The refusal existed and was correct; it
    # lived in a run log. A relocation that carried nine tenths of an agent
    # silently is discovered weeks later.
    # Act
    text = _record()
    # Assert
    assert "b68520e1-78fb" in text


def test_each_refusal_carries_its_reason() -> None:
    # Arrange: "not carried" without a reason reads as a bug rather than a
    # decision, and sends someone to re-run the copy.
    # Act
    text = _record()
    # Assert
    assert "a credential is never carried" in text


def test_an_empty_refusal_list_says_so_rather_than_printing_nothing() -> None:
    # Arrange: a blank section is indistinguishable from a section that was
    # never filled in. "Everything travelled" is a claim worth making.
    # Act
    text = _record(refused=())
    # Assert
    assert "Nothing was refused" in text


# --------------------------------------------------------------------------
# What did travel
# --------------------------------------------------------------------------


def test_the_record_lists_the_carried_memory_directory() -> None:
    # Arrange: the thing that was missing. An agent that finds memory/ empty
    # should be able to tell from this file whether it was supposed to be there.
    # Act
    text = _record()
    # Assert
    assert "`memory/`" in text


def test_the_transcript_is_listed_with_the_counts_as_carried() -> None:
    # Arrange: the SNAPSHOT numbers, not the source's current size — that is
    # what the target's file is supposed to weigh, and the only figure a manual
    # comparison can use.
    # Act
    text = _record()
    # Assert
    assert "108412278 bytes / 58325 lines" in text


def test_the_record_warns_that_a_half_written_final_record_may_be_missing() -> None:
    # Arrange: the accepted trade of the snapshot, said where the person who
    # would notice it reads.
    # Act
    text = _record()
    # Assert
    assert "LAST COMPLETE" in text


def test_carrying_nothing_is_stated_bluntly() -> None:
    # Arrange: the failure shape this whole feature exists to prevent, arriving
    # as a record. It must not read as an ordinary empty section.
    # Act
    text = _record(transcripts=(), directories=())
    # Assert
    assert "worth reading twice" in text


# --------------------------------------------------------------------------
# What was displaced
# --------------------------------------------------------------------------


def test_a_displaced_prior_directory_is_named() -> None:
    # Arrange: the target may have been this agent's home before. Its previous
    # contents were moved aside, never deleted, and the reader is the one who
    # decides whether to merge them back.
    # Act
    text = _record(displaced_to="/home/agent/.claude/projects/.old/20260812T0000Z/-p")
    # Assert
    assert "/home/agent/.claude/projects/.old/20260812T0000Z/-p" in text


def test_the_displacement_explains_that_source_won_by_rule_not_by_judgement() -> None:
    # Arrange: 「ソースの方が普通大切だろう」 — a default, not an assessment of
    # the contents. A reader must not conclude the older copy was worthless.
    # Act
    text = _record(displaced_to="/somewhere/.old/S/-p")
    # Assert
    assert "not a judgement about the contents" in text


def test_no_displacement_section_when_the_destination_was_empty() -> None:
    # Arrange: the common case must not manufacture a section about a directory
    # that never existed.
    # Act
    text = _record()
    # Assert
    assert "What was already here" not in text


def test_the_filename_is_the_one_the_transport_writes() -> None:
    # Arrange: an agent can only be told "read RELOCATED-FROM.md" if that is
    # actually where it lands. Pinned so the two cannot drift apart.
    # Act
    name = PROVENANCE_FILENAME
    # Assert
    assert name == "RELOCATED-FROM.md"
