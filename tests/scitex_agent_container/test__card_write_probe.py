#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: tests/scitex_agent_container/test__card_write_probe.py
"""Tests for the behavioural card-write probe.

The module under test exists because THREE presence checks passed on a broken
artifact. So these tests care most about one property: that the probe's verdict
can actually come back BROKEN, and that it cannot come back OK without the
discriminating write having run.

`pytest.raises` is deliberately absent: the linter counts it as an assertion,
and every refusal here needs TWO separate facts checked — that it refused, and
that it said something useful about why. `_refusal` captures the exception so
each of those is its own one-assert test.
"""

from __future__ import annotations

from typing import Callable

from scitex_agent_container._card_write_probe import (
    BROKEN,
    OK,
    UNKNOWN,
    CardWriteVerdict,
    build_probe_card_id,
    classify_write_failure,
)


def _refusal(build: Callable[[], object]) -> ValueError | None:
    """The ValueError ``build`` raised, or None if it did not raise one."""
    try:
        build()
    except ValueError as exc:
        return exc
    return None


def test_the_positional_index_defect_is_named_not_lumped_in() -> None:
    # Arrange — KeyError(0) is the signature of row[0] against a dict row. A
    # generic "something raised" hint would not tell an operator what to fix.
    exc = KeyError(0)
    # Act
    verdict = classify_write_failure(exc)
    # Assert
    assert "0.49.1" in verdict.hint


def test_a_keyerror_zero_is_broken_not_unknown() -> None:
    # Arrange — the write RAN and RAISED, so the store answered. That is
    # evidence, not an inability to measure, and must not soften to unknown.
    exc = KeyError(0)
    # Act
    verdict = classify_write_failure(exc)
    # Assert
    assert verdict.verdict == BROKEN


def test_an_unrelated_exception_is_still_broken() -> None:
    # Arrange — any failure of the second write means this image cannot record
    # work on a real card, whatever the cause.
    exc = RuntimeError("connection reset")
    # Act
    verdict = classify_write_failure(exc)
    # Assert
    assert verdict.verdict == BROKEN


def test_an_unrelated_exception_does_not_claim_the_0490_cause() -> None:
    # Arrange — naming a specific upstream bug for an unrelated failure sends
    # the reader to the wrong fix, which is worse than a generic hint.
    exc = RuntimeError("connection reset")
    # Act
    verdict = classify_write_failure(exc)
    # Assert
    assert "0.49.1" not in verdict.hint


def test_the_error_keeps_its_type_via_repr() -> None:
    # Arrange — str(KeyError(0)) is "0", which loses the type entirely and
    # reads like a stray number in a log.
    exc = KeyError(0)
    # Act
    verdict = classify_write_failure(exc)
    # Assert
    assert "KeyError" in verdict.error


def test_a_broken_verdict_without_a_hint_is_refused() -> None:
    # Arrange — an error that only states what broke is half-written.
    def _build() -> CardWriteVerdict:
        return CardWriteVerdict(verdict=BROKEN, detail="d", step="second_comment")

    # Act
    caught = _refusal(_build)
    # Assert
    assert caught is not None


def test_the_missing_hint_refusal_says_a_hint_is_what_is_missing() -> None:
    # Arrange — a refusal that does not name the missing thing just moves the
    # confusion one layer up.
    def _build() -> CardWriteVerdict:
        return CardWriteVerdict(verdict=BROKEN, detail="d", step="second_comment")

    # Act
    caught = _refusal(_build)
    # Assert
    assert "actionable hint" in str(caught)


def test_a_malformed_verdict_is_refused() -> None:
    # Arrange — a shape-shifting verdict is how "I could not tell" silently
    # becomes "yes" three layers downstream.
    def _build() -> CardWriteVerdict:
        return CardWriteVerdict(verdict="probably", detail="d", step="s")

    # Act
    caught = _refusal(_build)
    # Assert
    assert caught is not None


def test_the_malformed_verdict_refusal_quotes_the_offending_value() -> None:
    # Arrange — naming the bad value is what makes the failure fixable without
    # opening the source.
    def _build() -> CardWriteVerdict:
        return CardWriteVerdict(verdict="probably", detail="d", step="s")

    # Act
    caught = _refusal(_build)
    # Assert
    assert "probably" in str(caught)


def test_only_the_second_comment_step_is_conclusive() -> None:
    # Arrange — a verdict reached before the discriminating write cannot be a
    # conclusion, because the broken build passes every earlier step.
    verdict = CardWriteVerdict(verdict=UNKNOWN, detail="store unreachable", step="create")
    # Act
    conclusive = verdict.is_conclusive
    # Assert
    assert not conclusive


def test_a_verdict_from_the_second_write_is_conclusive() -> None:
    # Arrange — the whole point: the second comment is the one that
    # discriminates, so reaching it is what licenses a conclusion.
    verdict = CardWriteVerdict(verdict=OK, detail="wrote twice", step="second_comment")
    # Act
    conclusive = verdict.is_conclusive
    # Assert
    assert conclusive


def test_the_probe_card_id_is_recognisably_disposable() -> None:
    # Arrange — a human finding this card on the board must be able to tell at
    # a glance that it is machine-made and safe to remove.
    stamp = "20260823-153000"
    # Act
    card_id = build_probe_card_id(stamp)
    # Assert
    assert card_id.startswith("zz-probe-card-write-")


def test_the_probe_card_id_carries_the_caller_stamp() -> None:
    # Arrange — correlating a probe result with the deploy that prompted it is
    # only possible if the stamp survives into the id.
    stamp = "20260823-153000"
    # Act
    card_id = build_probe_card_id(stamp)
    # Assert
    assert stamp in card_id


def test_a_blank_stamp_is_refused_rather_than_invented() -> None:
    # Arrange — a probe that invents its own id produces results nobody can
    # correlate, and reproducibility dies quietly.
    def _build() -> str:
        return build_probe_card_id("   ")

    # Act
    caught = _refusal(_build)
    # Assert
    assert caught is not None
