"""A store that opens, parses and answers can still be the wrong store.

2026-08-08: a dotfiles deploy replaced this agent's `~/.scitex` — its live state
root — with a symlink into a git worktree and moved the real tree aside. The
agent booted, resolved its message store inside the substituted tree, found 149
rows whose newest was a MONTH old, and reported healthy. Nothing checked the one
thing that would have said so.

These tests pin that check. It is deliberately about TIME, not paths: a path
check would need to know about symlinks, overlays, binds and worktrees and would
still miss the next mechanism. "The newest thing in here is far older than my
own start" is true of all of them and needs to know about none.

The two failure directions it must NOT have:
  * shouting on a first boot (empty store) or on a legitimately quiet agent, and
  * folding "could not read" into a pass, which is how the original incident
    stayed invisible.

Pure, explicit timestamps, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state._store_freshness import (
    CODE_EMPTY,
    CODE_FRESH,
    CODE_FUTURE,
    CODE_STALE,
    CODE_UNKNOWN,
    DEFAULT_STALE_AFTER_S,
    FreshnessVerdict,
    assess_store_freshness,
)

BOOT = 1_000_000.0
HOUR = 3600.0
LABEL = "claude-code-telegrammer store"


def _assess(**overrides: object) -> FreshnessVerdict:
    facts: dict[str, object] = dict(
        newest_row_ts=BOOT - HOUR,
        process_started_at=BOOT,
        store_label=LABEL,
    )
    facts.update(overrides)
    return assess_store_freshness(**facts)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the incident this exists for
# ---------------------------------------------------------------------------


def test_a_month_old_newest_row_is_flagged() -> None:
    # Arrange: the measured shape of 2026-08-08 — the store resolved fine and
    # its newest row predated the boot by a month.
    verdict = _assess(newest_row_ts=BOOT - 30 * 24 * HOUR)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is False


def test_the_stale_verdict_names_the_substitution_shapes() -> None:
    # Arrange: an error that only states what broke is half-written. The reader
    # needs to know what class of thing to look for.
    verdict = _assess(newest_row_ts=BOOT - 30 * 24 * HOUR)
    # Act
    reason = verdict.reason
    # Assert
    assert "readlink -f" in reason


def test_the_stale_verdict_is_coded_distinctly() -> None:
    # Arrange
    verdict = _assess(newest_row_ts=BOOT - 30 * 24 * HOUR)
    # Act
    code = verdict.code
    # Assert
    assert code == CODE_STALE


def test_the_age_is_reported_for_the_caller_to_log() -> None:
    # Arrange
    verdict = _assess(newest_row_ts=BOOT - 48 * HOUR)
    # Act
    age = verdict.age_s
    # Assert
    assert age == 48 * HOUR


# ---------------------------------------------------------------------------
# it must not cry wolf
# ---------------------------------------------------------------------------


def test_a_recently_written_store_passes() -> None:
    # Arrange
    verdict = _assess()
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is True


def test_an_agent_idle_overnight_still_passes() -> None:
    # Arrange: a quiet agent legitimately has an old newest row. Flagging this
    # would make the check noise, and noise is how a real alarm gets ignored.
    verdict = _assess(newest_row_ts=BOOT - 12 * HOUR)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is True


def test_the_default_threshold_is_a_full_day() -> None:
    # Arrange: documenting the judgement rather than leaving it implicit.
    threshold = DEFAULT_STALE_AFTER_S
    # Act
    hours = threshold / HOUR
    # Assert
    assert hours == 24.0


def test_a_caller_that_knows_its_cadence_can_tighten_the_threshold() -> None:
    # Arrange: a chatty agent's store going quiet for an hour IS suspicious.
    verdict = _assess(newest_row_ts=BOOT - 2 * HOUR, stale_after_s=HOUR)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is False


def test_an_empty_store_is_not_a_fault() -> None:
    # Arrange: a new agent's first boot must not shout.
    verdict = _assess(newest_row_ts=None, had_rows=False)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is True


def test_an_empty_store_is_coded_apart_from_a_healthy_one() -> None:
    # Arrange: "nothing to compare" and "compared, looks right" are different
    # facts, and a caller may want to treat them differently.
    verdict = _assess(newest_row_ts=None, had_rows=False)
    # Act
    code = verdict.code
    # Assert
    assert code == CODE_EMPTY


# ---------------------------------------------------------------------------
# unknown is not a pass
# ---------------------------------------------------------------------------


def test_rows_exist_but_unreadable_timestamp_is_unknown() -> None:
    # Arrange: a store that cannot answer this is not a store known to be
    # healthy. Folding it into a pass is how the original incident hid.
    verdict = _assess(newest_row_ts=None, had_rows=True)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is None


def test_an_uninspected_store_is_unknown() -> None:
    # Arrange: had_rows=None means nobody looked.
    verdict = _assess(newest_row_ts=None, had_rows=None)
    # Act
    code = verdict.code
    # Assert
    assert code == CODE_UNKNOWN


def test_the_unknown_verdict_says_why_it_matters() -> None:
    # Arrange
    verdict = _assess(newest_row_ts=None, had_rows=True)
    # Act
    reason = verdict.reason
    # Assert
    assert "not a store known to be healthy" in reason


# ---------------------------------------------------------------------------
# a row from the future means another writer, or a clock
# ---------------------------------------------------------------------------


def test_a_row_newer_than_this_process_is_flagged() -> None:
    # Arrange: either a clock disagreement or a second writer, and both mean
    # this process is not alone — which is the split-brain shape.
    verdict = _assess(newest_row_ts=BOOT + HOUR)
    # Act
    fresh = verdict.fresh
    # Assert
    assert fresh is False


def test_a_future_row_is_coded_apart_from_a_stale_one() -> None:
    # Arrange: opposite causes, opposite investigations.
    verdict = _assess(newest_row_ts=BOOT + HOUR)
    # Act
    code = verdict.code
    # Assert
    assert code == CODE_FUTURE


def test_a_future_row_names_the_two_possible_causes() -> None:
    # Arrange
    verdict = _assess(newest_row_ts=BOOT + HOUR)
    # Act
    reason = verdict.reason
    # Assert
    assert "another writer" in reason


# ---------------------------------------------------------------------------
# the shape validates itself
# ---------------------------------------------------------------------------


def test_a_pass_carrying_a_failure_code_is_rejected() -> None:
    # Arrange: the shape that lets a caller reading one field conclude the
    # opposite of the other.
    fields = dict(fresh=True, code=CODE_STALE, reason="contradictory")

    # Act
    def build() -> FreshnessVerdict:
        return FreshnessVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_an_unknown_carrying_a_pass_code_is_rejected() -> None:
    # Arrange
    fields = dict(fresh=None, code=CODE_FRESH, reason="contradictory")

    # Act
    def build() -> FreshnessVerdict:
        return FreshnessVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_an_empty_reason() -> None:
    # Arrange
    fields = dict(fresh=False, code=CODE_STALE, reason="")

    # Act
    def build() -> FreshnessVerdict:
        return FreshnessVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_suspicious_verdict_is_truthy_so_callers_must_read_the_field() -> None:
    # Arrange: deliberately no __bool__, so `if verdict:` cannot read as an
    # all-clear for a substituted store. Documents the trap rather than hiding it.
    verdict = _assess(newest_row_ts=BOOT - 30 * 24 * HOUR)
    # Act
    truthy = bool(verdict)
    # Assert
    assert truthy is True
