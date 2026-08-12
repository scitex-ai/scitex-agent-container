"""Tests for ``_lifecycle._verdict_refusal_read`` (PS-204 mirror).

NO MOCKS, AND THE REFUSALS ARE NOT SIMULATED. Every "unable to act" assertion
below runs against a transcript record captured VERBATIM from the host's own
``~/.claude/projects`` store — the real bytes Claude Code wrote at the moment
the agent could not act. The three under ``fixtures/refusals/`` are the two
incidents on the card:

  * ``quota_weekly_limit_20260810.jsonl`` — 2026-08-10T14:41:46Z,
    "You've hit your weekly limit · resets 11pm (UTC)". THE reported incident.
  * ``not_logged_in_20260810.jsonl``      — 2026-08-10T16:21:40Z,
    "Not logged in · Please run /login".
  * ``oauth_401_expired_20260810.jsonl``  — 2026-08-10T11:53:08Z,
    "Failed to authenticate. API Error: 401 OAuth access token has expired."

and the negative case is equally real: ``clean_turn_20260810.jsonl`` is an
ordinary ``claude-opus-5`` turn with real token usage. Fabricating either side
would have tested the fixture author's idea of a refusal rather than the thing
that actually stops work.

Reads are driven against real files on disk (``tmp_path``), a real clock value
passed in as a plain float. STX-TQ002 AAA markers + STX-TQ007 one observable
assert + STX-TQ003 descriptive names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._verdict_refusal_read import (
    CAUSE_CREDENTIALS,
    CAUSE_QUOTA,
    CAUSE_UNCLASSIFIED,
    STATE_CLEAN,
    STATE_REFUSED,
    STATE_UNKNOWN,
    RefusalRead,
    classify_refusal,
    last_turn_refusal,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "refusals"

# The real stamps on the captured records, as epoch seconds, so freshness tests
# are deterministic rather than dependent on when the suite runs.
_QUOTA_FIXTURE = _FIXTURES / "quota_weekly_limit_20260810.jsonl"
_NOT_LOGGED_IN_FIXTURE = _FIXTURES / "not_logged_in_20260810.jsonl"
_OAUTH_FIXTURE = _FIXTURES / "oauth_401_expired_20260810.jsonl"
_CLEAN_FIXTURE = _FIXTURES / "clean_turn_20260810.jsonl"


def _at(fixture: Path) -> float:
    """The real timestamp on a captured record, as epoch seconds."""
    return float(last_turn_refusal(fixture, now=0.0, stale_after_s=1e12).at or 0.0)


# --- classify_refusal — naming the cause -----------------------------------


def test_classify_names_quota_for_the_real_weekly_limit_message() -> None:
    # Arrange
    text = "You've hit your weekly limit · resets 11pm (UTC)"
    # Act
    cause, _remedy = classify_refusal(text)
    # Assert
    assert cause == CAUSE_QUOTA


def test_classify_names_credentials_for_the_real_not_logged_in_message() -> None:
    # Arrange
    text = "Not logged in · Please run /login"
    # Act
    cause, _remedy = classify_refusal(text)
    # Assert
    assert cause == CAUSE_CREDENTIALS


def test_classify_names_credentials_for_the_real_401_oauth_message() -> None:
    # Arrange
    text = "Failed to authenticate. API Error: 401 OAuth access token has expired."
    # Act
    cause, _remedy = classify_refusal(text)
    # Assert
    assert cause == CAUSE_CREDENTIALS


def test_classify_falls_back_to_unclassified_rather_than_calling_it_clean() -> None:
    # Arrange
    text = "the provider said something nobody has seen before"
    # Act
    cause, _remedy = classify_refusal(text)
    # Assert
    assert cause == CAUSE_UNCLASSIFIED


def test_classify_quota_remedy_says_a_restart_does_not_fix_it() -> None:
    # Arrange
    text = "You've hit your weekly limit · resets 11pm (UTC)"
    # Act
    _cause, remedy = classify_refusal(text)
    # Assert
    assert "RESTART DOES NOT FIX THIS" in remedy


def test_classify_credentials_remedy_names_the_reauthentication_command() -> None:
    # Arrange
    text = "Not logged in · Please run /login"
    # Act
    _cause, remedy = classify_refusal(text)
    # Assert
    assert "sac accounts login" in remedy


# --- last_turn_refusal — the REAL captured refusals -------------------------


def test_real_weekly_limit_transcript_reads_as_refused() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert read.state == STATE_REFUSED


def test_real_weekly_limit_transcript_names_quota_as_the_cause() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert read.cause == CAUSE_QUOTA


def test_real_weekly_limit_transcript_quotes_the_providers_own_words() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert read.text == "You've hit your weekly limit · resets 11pm (UTC)"


def test_real_weekly_limit_detail_says_the_agent_cannot_act() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert "PRESENT but CANNOT ACT" in read.detail


def test_real_weekly_limit_detail_names_the_evidence_file() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert str(_QUOTA_FIXTURE) in read.detail


def test_real_not_logged_in_transcript_reads_as_refused() -> None:
    # Arrange
    now = _at(_NOT_LOGGED_IN_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_NOT_LOGGED_IN_FIXTURE, now=now)
    # Assert
    assert read.state == STATE_REFUSED


def test_real_not_logged_in_transcript_names_credentials_as_the_cause() -> None:
    # Arrange
    now = _at(_NOT_LOGGED_IN_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_NOT_LOGGED_IN_FIXTURE, now=now)
    # Assert
    assert read.cause == CAUSE_CREDENTIALS


def test_real_oauth_401_transcript_names_credentials_as_the_cause() -> None:
    # Arrange
    now = _at(_OAUTH_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_OAUTH_FIXTURE, now=now)
    # Assert
    assert read.cause == CAUSE_CREDENTIALS


# --- last_turn_refusal — the REAL clean turn (no false positive) ------------


def test_real_ordinary_turn_reads_as_clean() -> None:
    # Arrange
    now = _at(_CLEAN_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_CLEAN_FIXTURE, now=now)
    # Assert
    assert read.state == STATE_CLEAN


def test_real_clean_turn_names_no_cause() -> None:
    # Arrange
    now = _at(_CLEAN_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_CLEAN_FIXTURE, now=now)
    # Assert
    assert read.cause == ""


def test_clean_detail_refuses_to_claim_the_agent_is_alive() -> None:
    # Arrange
    now = _at(_CLEAN_FIXTURE) + 10.0
    # Act
    read = last_turn_refusal(_CLEAN_FIXTURE, now=now)
    # Assert
    assert "Not proof of life" in read.detail


# --- the ERA rule: a refusal the agent recovered from is not current --------


def test_a_refusal_followed_by_a_real_turn_reads_clean_not_refused(
    tmp_path: Path,
) -> None:
    # Arrange — the REAL quota refusal, then the REAL ordinary turn after it.
    transcript = tmp_path / "recovered.jsonl"
    transcript.write_text(
        _QUOTA_FIXTURE.read_text(encoding="utf-8")
        + _CLEAN_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Act
    read = last_turn_refusal(transcript, now=_at(_CLEAN_FIXTURE) + 10.0)
    # Assert
    assert read.state == STATE_CLEAN


def test_a_stale_refusal_is_unknown_never_a_standing_accusation() -> None:
    # Arrange — the real refusal, read a full day later.
    now = _at(_QUOTA_FIXTURE) + 86_400.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert read.state == STATE_UNKNOWN


def test_a_stale_refusal_explains_that_idle_and_unable_are_indistinguishable() -> None:
    # Arrange
    now = _at(_QUOTA_FIXTURE) + 86_400.0
    # Act
    read = last_turn_refusal(_QUOTA_FIXTURE, now=now)
    # Assert
    assert "not been asked since" in read.detail


# --- absence of evidence is UNKNOWN, never CLEAN ---------------------------


def test_a_missing_transcript_is_unknown_not_clean(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "nothing-here.jsonl"
    # Act
    read = last_turn_refusal(missing, now=0.0)
    # Assert
    assert read.state == STATE_UNKNOWN


def test_a_missing_transcript_names_the_path_it_could_not_read(
    tmp_path: Path,
) -> None:
    # Arrange
    missing = tmp_path / "nothing-here.jsonl"
    # Act
    read = last_turn_refusal(missing, now=0.0)
    # Assert
    assert str(missing) in read.detail


def test_a_transcript_with_no_assistant_turn_is_unknown(tmp_path: Path) -> None:
    # Arrange
    transcript = tmp_path / "user-only.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n', "utf-8")
    # Act
    read = last_turn_refusal(transcript, now=0.0)
    # Assert
    assert read.state == STATE_UNKNOWN


def test_a_tail_slice_beginning_mid_record_does_not_crash_the_read(
    tmp_path: Path,
) -> None:
    # Arrange — a deliberately truncated leading line, then the real refusal.
    transcript = tmp_path / "truncated.jsonl"
    transcript.write_text(
        '{"type":"assistant","mess\n' + _QUOTA_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Act
    read = last_turn_refusal(transcript, now=_at(_QUOTA_FIXTURE) + 10.0)
    # Assert
    assert read.state == STATE_REFUSED


# --- the RefusalRead validator ---------------------------------------------


def test_an_unknown_state_string_is_refused_at_construction() -> None:
    # Arrange
    bad_state = "healthy"
    # Act
    guard = pytest.raises(ValueError, match="must be one of")
    # Assert
    with guard:
        RefusalRead(state=bad_state, detail="whatever")


def test_a_refused_read_without_a_cause_is_refused_at_construction() -> None:
    # Arrange
    state = STATE_REFUSED
    # Act
    guard = pytest.raises(ValueError, match="must name a cause")
    # Assert
    with guard:
        RefusalRead(state=state, detail="refused but says nothing about why")
