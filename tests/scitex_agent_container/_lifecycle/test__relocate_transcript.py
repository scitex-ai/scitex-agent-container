#!/usr/bin/env python3
"""A copy that returned 0 is not a transcript the agent will find at boot.

Measured 2026-08-08: inside a container `~/.scitex` was a symlink into a dotfiles
git worktree, created mid-session. Every write SUCCEEDED and the bytes landed
where a `git clean -xdf` erases them. So the property under test is not "did the
copy work" but "is it readable ON THE TARGET, and is it the same content".

The sharpest test here is the one where the send SUCCEEDS and the read-back
FAILS. That is the 2026-08-07 relocation exactly — an agent that booted healthy
with no memory of the conversation that moved it — and the outcome must be
UNKNOWN, never carried.

Real callables raising real exceptions. No mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_transcript import (
    CODE_CARRIED,
    CODE_MISMATCH,
    CODE_NOTHING_TO_CARRY,
    CODE_SOURCE_UNREADABLE,
    CODE_UNVERIFIABLE,
    CarryOutcome,
    carry_transcript,
    digest,
)

PAYLOAD = b'{"type":"user","text":"the conversation that moved it"}\n' * 40


def _landed(store: dict):
    """A target that keeps whatever was sent — the happy transport."""

    def send(payload: bytes) -> None:
        store["bytes"] = payload

    def read_back() -> bytes:
        return store["bytes"]

    return send, read_back


def _ok_source() -> bytes:
    return PAYLOAD


def _boom():
    raise OSError("no route to host")


def test_a_verified_copy_reports_carried() -> None:
    # Arrange: the positive control. Without it the refusal tests below could
    # pass because the fixture is broken rather than because refusal works.
    send, read_back = _landed({})
    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.carried is True


def test_a_verified_copy_carries_the_success_code() -> None:
    # Arrange: callers branch on the code, not on prose.
    send, read_back = _landed({})
    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.code == CODE_CARRIED


def test_the_reported_digest_is_the_one_read_back_from_the_target() -> None:
    # Arrange: reporting the SOURCE digest would let a success be claimed
    # without the target ever being consulted.
    send, read_back = _landed({})
    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.target_digest == digest(PAYLOAD)


def test_a_send_that_lands_but_cannot_be_read_back_is_unknown() -> None:
    # Arrange: THE case this module exists for. The write succeeded; whether the
    # agent will find it is unanswered.
    def send(_: bytes) -> None:
        return None

    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=_boom
    )
    # Assert
    assert out.carried is None


def test_an_unverifiable_copy_is_not_reported_as_carried() -> None:
    # Arrange: the same state, stated as the negative — an unknown must never
    # be softened into a yes on the way out.
    def send(_: bytes) -> None:
        return None

    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=_boom
    )
    # Assert
    assert out.code == CODE_UNVERIFIABLE


def test_a_content_mismatch_is_refused() -> None:
    # Arrange: the target holds something of its own — plausible, because a
    # relocation target may have been this agent's home before.
    def send(_: bytes) -> None:
        return None

    def read_back() -> bytes:
        return b"a different transcript entirely"

    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.code == CODE_MISMATCH


def test_a_mismatch_is_a_decided_no_not_an_unknown() -> None:
    # Arrange: we DID reach the target and it disagreed. That is an answer, and
    # it calls for a different action than "could not check".
    def send(_: bytes) -> None:
        return None

    def read_back() -> bytes:
        return b"a different transcript entirely"

    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.carried is False


def test_a_same_length_but_different_payload_is_still_caught() -> None:
    # Arrange: this is why the check is a digest and not a size. A truncated and
    # re-padded file has the right length.
    def send(_: bytes) -> None:
        return None

    def read_back() -> bytes:
        return b"X" * len(PAYLOAD)

    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=send, read_back=read_back
    )
    # Assert
    assert out.code == CODE_MISMATCH


def test_a_failed_send_is_unknown_not_a_clean_no() -> None:
    # Arrange: bytes may have partially landed; only the target can say.
    # Reporting "not carried" would invite a retry onto a half-written file.
    # Act
    out = carry_transcript(
        carry=True, read_source=_ok_source, send=_boom, read_back=_boom
    )
    # Assert
    assert out.carried is None


def test_an_unreadable_source_names_its_own_outcome() -> None:
    # Arrange: nothing was sent, so this is a decided no — distinct from a send
    # that may have half-landed.
    send, read_back = _landed({})
    # Act
    out = carry_transcript(
        carry=True, read_source=_boom, send=send, read_back=read_back
    )
    # Assert
    assert out.code == CODE_SOURCE_UNREADABLE


def test_the_failure_reason_carries_the_underlying_error() -> None:
    # Arrange: "could not read" without WHY turns a five-second fix into an
    # investigation.
    send, read_back = _landed({})
    # Act
    out = carry_transcript(
        carry=True, read_source=_boom, send=send, read_back=read_back
    )
    # Assert
    assert "no route to host" in out.reason


def test_a_declined_plan_copies_nothing() -> None:
    # Arrange: --no-carry-session, or a target that already diverged.
    def send(_: bytes) -> None:
        raise AssertionError("send must not run for a declined plan")

    # Act
    out = carry_transcript(
        carry=False, read_source=_ok_source, send=send, read_back=_boom
    )
    # Assert
    assert out.code == CODE_NOTHING_TO_CARRY


def test_an_undecided_plan_refuses_rather_than_guessing() -> None:
    # Arrange: `SeedPlan.carry is None` means the inputs did not answer. Acting
    # on it either way would guess about the agent's memory.
    def send(_: bytes) -> None:
        raise AssertionError("send must not run for an undecided plan")

    # Act
    out = carry_transcript(
        carry=None, read_source=_ok_source, send=send, read_back=_boom
    )
    # Assert
    assert out.carried is None


def test_a_success_without_a_target_digest_is_unrepresentable() -> None:
    # Arrange: the validator refuses to let anyone construct a "carried" outcome
    # that never consulted the target — the invariant lives in the type.
    fields = {
        "carried": True,
        "code": CODE_CARRIED,
        "reason": "x",
        "source_digest": "a",
    }
    # Act
    build = lambda: CarryOutcome(**fields)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_success_with_mismatched_digests_is_unrepresentable() -> None:
    # Arrange: same reasoning, the other way — a success may not disagree with
    # its own evidence.
    fields = {
        "carried": True,
        "code": CODE_CARRIED,
        "reason": "x",
        "source_digest": "a",
        "target_digest": "b",
    }
    # Act
    build = lambda: CarryOutcome(**fields)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


def test_the_outcome_defines_no_bool() -> None:
    # Arrange: `if outcome:` on an UNKNOWN would read as a yes, which is the
    # exact defect this module prevents. Python falls back to truthy for any
    # object, so the guard is that we never DEFINE __bool__ — pinned here so a
    # future convenience addition has to argue with a test.
    # Act
    defined = "__bool__" in vars(CarryOutcome)
    # Assert
    assert defined is False
