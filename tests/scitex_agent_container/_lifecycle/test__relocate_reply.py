"""The parser that must not mistake our own challenge for the target's answer."""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_arrival import build_arrival_brief
from scitex_agent_container._lifecycle._relocate_reply import read_reply

NONCE = "a1b2c3d4e5f60718"


def test_an_answer_alongside_the_nonce_is_read() -> None:
    # Arrange
    text = f'{{"role":"assistant","text":"nonce={NONCE} answer=scitex-compute-04"}}'
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert reading.answer == "scitex-compute-04"


def test_the_nonce_is_reported_seen_separately_from_the_answer() -> None:
    # Arrange: a correlated reply that never got round to answering. The gate
    # treats these as different facts, so the reading must too.
    text = f'{{"text":"nonce={NONCE} — working on it"}}'
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert (reading.nonce_seen, reading.answer) == (True, None)


def test_the_challenge_we_sent_is_not_read_as_an_answer() -> None:
    # Arrange: THE failure this module exists for. The brief contains the nonce
    # and the word answer=, because it is telling the agent what to send back.
    # A transcript search finds it BEFORE the agent has done anything at all.
    brief = build_arrival_brief(
        agent="canary",
        from_host="src",
        to_host="tgt",
        resume_session_id="1111",
        nonce=NONCE,
        question="run `hostname` and report it verbatim",
    )
    # Act: the whole brief on one line, as a jsonl record holds it.
    reading = read_reply(brief.replace("\n", "\\n"), nonce=NONCE)
    # Assert
    assert reading.answer is None


def test_the_discarded_challenge_is_counted_rather_than_silently_dropped() -> None:
    # Arrange: "I found only the question I asked" and "I found nothing at all"
    # look identical from the verdict and mean different things about delivery.
    brief = build_arrival_brief(
        agent="canary",
        from_host="src",
        to_host="tgt",
        resume_session_id="1111",
        nonce=NONCE,
        question="run `hostname` and report it verbatim",
    )
    # Act
    reading = read_reply(brief.replace("\n", "\\n"), nonce=NONCE)
    # Assert
    assert reading.placeholders_ignored >= 1


def test_a_real_answer_is_still_found_when_the_challenge_shares_the_line() -> None:
    # Arrange: the ORDINARY case on a live host — one jsonl record can hold the
    # echoed prompt and the reply, so discarding the placeholder must not
    # discard the answer sitting beside it.
    brief = build_arrival_brief(
        agent="canary",
        from_host="src",
        to_host="tgt",
        resume_session_id="1111",
        nonce=NONCE,
        question="run `hostname` and report it verbatim",
    )
    line = (
        brief.replace("\n", "\\n")
        + f'\\n{{"assistant":"nonce={NONCE}\\nanswer=nas-03"}}'
    )
    # Act
    reading = read_reply(line, nonce=NONCE)
    # Assert
    assert reading.answer == "nas-03"


def test_an_answer_correlated_to_a_different_nonce_is_not_read() -> None:
    # Arrange: a reply left over from an earlier attempt. Without correlation a
    # relocation retried three times eventually finds one that proves nothing.
    text = "nonce=deadbeefdeadbeef answer=some-other-host"
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert (reading.nonce_seen, reading.answer) == (False, None)


def test_a_json_escaped_answer_stops_at_the_closing_quote() -> None:
    # Arrange: the answer lives inside a JSON string literal on disk.
    text = f'{{"text":"nonce={NONCE}\\nanswer=scitex-compute-04\\n"}}'
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert reading.answer == "scitex-compute-04"


def test_the_latest_of_several_answers_wins() -> None:
    # Arrange: two attempts in one search result. The later text is the more
    # recent turn.
    text = f"nonce={NONCE} answer=first\nnonce={NONCE} answer=second"
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert reading.answer == "second"


def test_empty_text_is_not_a_reply() -> None:
    # Arrange: a channel that returned nothing at all.
    text = ""
    # Act
    reading = read_reply(text, nonce=NONCE)
    # Assert
    assert (reading.nonce_seen, reading.answer) == (False, None)


def test_an_empty_nonce_is_refused_rather_than_matching_everything() -> None:
    # Arrange: correlation is this function's entire job; a call with nothing to
    # correlate against has lost the property it came here for.
    text = "nonce= answer=anything"
    # Act
    call = lambda: read_reply(text, nonce="")  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="non-empty nonce"):
        call()
