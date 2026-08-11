#!/usr/bin/env python3
"""An agent that resumed on a new host and was not told is confidently wrong.

The operator's requirement (2026-08-11) is that the relocated agent be told three
things: where it moved FROM, where it moved TO, and the session id its memory was
resumed from. Each is asserted separately here rather than as one substring
match, so a rewording that drops one of them fails on the one it dropped and
names it.

The resume-id assertions are the load-bearing ones. An agent told "your memory
was carried" with no id cannot check the claim — and that exact claim was
silently false on 2026-08-07, which is why this feature exists at all.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_arrival import (
    DEFAULT_HANDOVER_DOC,
    build_arrival_brief,
)

SESSION = "0f2c9c1e-7b3a-4d55-9e21-6b1c0a4f8e33"
NONCE = "n-4417"
QUESTION = "run `hostname` and report exactly what it printed"


def _brief(**over) -> str:
    kwargs = {
        "agent": "scitex-lead",
        "from_host": "ywata-note-win",
        "to_host": "scitex-compute-04",
        "resume_session_id": SESSION,
        "nonce": NONCE,
        "question": QUESTION,
    }
    kwargs.update(over)
    return build_arrival_brief(**kwargs)


def test_the_brief_names_the_source_host() -> None:
    # Arrange: "you moved" without saying from where leaves the agent unable to
    # judge which of its remembered facts are now stale.
    # Act
    text = _brief()
    # Assert
    assert "ywata-note-win" in text


def test_the_brief_names_the_target_host() -> None:
    # Arrange: the other half of the same sentence.
    # Act
    text = _brief()
    # Assert
    assert "scitex-compute-04" in text


def test_the_brief_names_the_resumed_session_id() -> None:
    # Arrange: THE one handle that lets the agent verify its own continuity.
    # Act
    text = _brief()
    # Assert
    assert SESSION in text


def test_the_brief_names_the_agent() -> None:
    # Arrange: it is delivered over a shared bus; an unaddressed instruction is
    # one an agent can reasonably decide is not for it.
    # Act
    text = _brief()
    # Assert
    assert "scitex-lead" in text


def test_the_brief_warns_that_the_transcript_describes_the_old_host() -> None:
    # Arrange: the real failure mode. The conversation above the message is full
    # of paths and ports that were true elsewhere, and nothing in it says so.
    # Act
    text = _brief()
    # Assert
    assert "may not hold here" in text


def test_the_brief_points_at_the_handover_document() -> None:
    # Arrange: the operator's own notes for this move are state the agent needs
    # and cannot infer from its transcript.
    # Act
    text = _brief()
    # Assert
    assert DEFAULT_HANDOVER_DOC in text


def test_the_handover_document_pointer_is_overridable() -> None:
    # Arrange: the default names one dated migration; a later relocation must
    # not be told to read a stale file.
    # Act
    text = _brief(handover_doc="~/notes/move-2027.md")
    # Assert
    assert "~/notes/move-2027.md" in text


def test_the_brief_points_at_the_card_board_as_authoritative() -> None:
    # Arrange: the transcript's memory of what is in flight is exactly the thing
    # that is stale after a move; the board is not.
    # Act
    text = _brief()
    # Assert
    assert "scitex-todo" in text


def test_the_brief_carries_the_handshake_nonce() -> None:
    # Arrange: it IS the handshake challenge — without correlation, a reply left
    # over from an earlier turn satisfies "the agent answered".
    # Act
    text = _brief()
    # Assert
    assert NONCE in text


def test_the_brief_carries_the_proof_of_work_question() -> None:
    # Arrange: an echo proves the message path, not the agent, and this message
    # is the gate for the agent.
    # Act
    text = _brief()
    # Assert
    assert QUESTION in text


def test_the_brief_says_the_source_has_been_stopped() -> None:
    # Arrange: identity is 1 -> 1. An agent that believes a copy of it is still
    # running elsewhere will coordinate with something that is not there.
    # Act
    text = _brief()
    # Assert
    assert "has been stopped" in text


def test_a_brief_without_a_resume_id_is_refused() -> None:
    # Arrange: "your memory was carried" with nothing to check it against is the
    # claim that was silently false before. Refusing beats asserting it blindly.
    # Act
    build = lambda: _brief(resume_session_id="")  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_brief_without_a_nonce_is_refused() -> None:
    # Arrange: a gate that can be disabled by passing nothing is a gate that
    # will eventually be disabled by passing nothing.
    # Act
    build = lambda: _brief(nonce="")  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_brief_without_a_proof_of_work_question_is_refused() -> None:
    # Arrange: same reasoning, the other requirement.
    # Act
    build = lambda: _brief(question="")  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_move_from_a_host_to_itself_is_refused() -> None:
    # Arrange: a brief announcing a move that did not happen is a false
    # statement made to the agent, in the message it is most likely to trust.
    # Act
    build = lambda: _brief(from_host="h", to_host="h")  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()
