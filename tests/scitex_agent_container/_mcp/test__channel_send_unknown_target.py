"""A send to a name that was NEVER REGISTERED must say so, not queue it.

WHY THIS EXISTS. A ``delivered_subscriber_count`` of 0 has two causes that
demand OPPOSITE actions, and the count alone cannot tell them apart:

    registered agent, adapter momentarily detached  ->  WAIT; it replays
    name that was never registered (a typo)         ->  FIX THE NAME

Before this guard both returned the same payload — ``durably_queued: True``
plus remedy text saying, in those words, "Do NOT re-send this message".

MEASURED 2026-08-09 by scitex-dev: they addressed this agent as ``sac-04``
(real name ``scitex-agent-container-04``) ALL DAY. Every send returned 200
with ``durably_queued: true``, so they did not re-send — the guidance told
them not to. The messages went to a queue no adapter will ever attach to.
They discovered it only by checking the registry for an unrelated reason.

The dangerous half is not the failure, it is the CONFIDENT WRONG ADVICE: a
message that cannot arrive, reported as safely queued, with instructions not
to retry. These tests pin the distinction and the fallback that keeps it
honest when the registry cannot be read.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._mcp._channel_send_errors import (
    ERR_NO_SUBSCRIBER,
    ERR_UNKNOWN_TARGET,
    no_subscriber_error,
    suggest_names,
    unknown_target_error,
)

_KNOWN = [
    "scitex-agent-container-04",
    "scitex-dev",
    "scitex-hub",
    "scitex-storage",
]


def test_unknown_target_carries_its_own_failure_code():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    code = err.code
    # Assert — a caller must branch on the CLASS, not string-match prose.
    assert code == ERR_UNKNOWN_TARGET


def test_unknown_target_is_not_the_no_subscriber_code():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    code = err.code
    # Assert — collapsing these two is the entire bug.
    assert code != ERR_NO_SUBSCRIBER


def test_unknown_target_does_not_claim_the_message_is_queued():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    queued = err.detail["durably_queued"]
    # Assert — the load-bearing field. Claiming True is what made a real
    # message wait forever.
    assert queued is False


def test_detached_adapter_still_claims_durable_queueing():
    # Arrange — the OTHER case must keep its existing promise.
    err = no_subscriber_error("scitex-dev")
    # Act
    queued = err.detail["durably_queued"]
    # Assert
    assert queued is True


def test_unknown_target_suggests_the_real_name():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    suggestions = err.detail["suggestions"]
    # Assert — a typo should cost seconds, not an indefinite wait.
    assert "scitex-agent-container-04" in suggestions


def test_unknown_target_names_the_real_name_in_the_message():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    text = str(err)
    # Assert — the human sentence must carry it too; not every caller
    # reads `detail`.
    assert "scitex-agent-container-04" in text


def test_unknown_target_tells_the_caller_to_re_send():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    advice = " ".join(err.detail["what_to_do"]).lower()
    # Assert — the exact inversion of the no_subscriber advice.
    assert "re-send" in advice


def test_detached_adapter_tells_the_caller_not_to_re_send():
    # Arrange
    err = no_subscriber_error("scitex-dev")
    # Act
    advice = " ".join(err.detail["what_to_do"]).lower()
    # Assert
    assert "do not re-send" in advice


def test_plain_difflib_would_have_missed_the_real_case():
    # Arrange — the reason suggest_names exists rather than a one-liner.
    import difflib

    # Act
    naive = difflib.get_close_matches("sac-04", _KNOWN, n=3, cutoff=0.4)
    # Assert — character similarity calls these unrelated strings. A future
    # refactor back to plain difflib silently reintroduces the miss, so pin
    # the inadequacy itself.
    assert "scitex-agent-container-04" not in naive


def test_acronym_of_a_registered_name_is_suggested():
    # Arrange — 'sac' is the initials of scitex-agent-container; this is the
    # house naming convention and the most likely way a name goes wrong.
    # Act
    suggestions = suggest_names("sac", _KNOWN)
    # Assert
    assert "scitex-agent-container-04" in suggestions


def test_a_shared_instance_suffix_is_suggested():
    # Arrange — right instance, wrong package name.
    # Act
    suggestions = suggest_names("wrongname-04", _KNOWN)
    # Assert
    assert "scitex-agent-container-04" in suggestions


def test_an_ordinary_typo_is_still_suggested():
    # Arrange — character similarity must keep working.
    # Act
    suggestions = suggest_names("scitex-hubb", _KNOWN)
    # Assert
    assert "scitex-hub" in suggestions


def test_an_unrelated_name_is_not_suggested():
    # Arrange — a suggester that matches everything is noise.
    # Act
    suggestions = suggest_names("zzzzzz", _KNOWN)
    # Assert
    assert suggestions == []


def test_a_name_resembling_nothing_yields_no_suggestions():
    # Arrange — nothing in the registry resembles this.
    err = unknown_target_error("zzzzzz", _KNOWN)
    # Act
    suggestions = err.detail["suggestions"]
    # Assert — a bad guess would be worse than none.
    assert suggestions == []


def test_a_name_resembling_nothing_still_points_at_the_registry():
    # Arrange
    err = unknown_target_error("zzzzzz", _KNOWN)
    # Act
    text = str(err)
    # Assert — with no suggestion to offer, say where to look instead.
    assert "a2a_peers" in text or "registered" in text


def test_an_empty_registry_yields_no_suggestions():
    # Arrange — the registry read came back with nothing at all.
    err = unknown_target_error("sac-04", [])
    # Act
    suggestions = err.detail["suggestions"]
    # Assert
    assert suggestions == []


def test_an_empty_registry_still_produces_a_message():
    # Arrange
    err = unknown_target_error("sac-04", [])
    # Act
    text = str(err)
    # Assert — must degrade to a sentence, not an exception.
    assert text


@pytest.mark.parametrize("code", [ERR_UNKNOWN_TARGET, ERR_NO_SUBSCRIBER])
def test_both_failure_modes_report_not_delivered(code):
    # Arrange
    err = (
        unknown_target_error("sac-04", _KNOWN)
        if code == ERR_UNKNOWN_TARGET
        else no_subscriber_error("scitex-dev")
    )
    # Act
    delivered = err.detail["delivered"]
    # Assert — they differ on recoverability, never on delivery.
    assert delivered is False
