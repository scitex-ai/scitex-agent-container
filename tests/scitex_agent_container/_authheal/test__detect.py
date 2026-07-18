"""The READ-ONLY detector: which live agents are CORROBORATED login-expired.

``detect_login_expired`` is the gate in front of every restart, so its whole
job is to be conservative: it fires ONLY on a banner that is frozen across
the two captures (the real ``evaluate_agents`` matcher runs here, unmocked),
and it must reject a banner that moved, a clean pane, and an unreadable pane.
A false positive here is a needless restart that destroys a working agent's
context, so these are the tests that keep the auto-restarter safe.

Rejecting is not the same as forgetting, and the second half of this suite
pins that difference: an unreadable pane must stay OUT of ``auth_failed`` and
must still turn up in ``unknown``, because the agent nobody could read is the
one nobody is watching. The roster tests do the same for the population — a
registry we cannot enumerate must not read as a registry with nobody in it.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._authheal._detect import (
    detect_login_expired,
    registered_agents,
)

from ._helpers import OK, register_agents, stuck, transient


def test_frozen_banner_is_corroborated_login_expired():
    # Arrange — a banner identical on both reads = frozen = the real thing.
    captures = stuck("scitex-hpc")
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.auth_failed == ("scitex-hpc",)


def test_single_run_transient_banner_is_not_flagged():
    # Arrange — a banner on run 1 that is GONE on the decisive run 2. The
    # agent is producing output (working or merely quoting the incident), so
    # it is NOT wedged and must never be restarted.
    captures = transient("figrecipe")
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.auth_failed == ()


def test_clean_pane_is_not_flagged():
    # Arrange — no banner at all on either read.
    captures = {"writer": (OK, OK)}
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.auth_failed == ()


def test_uncapturable_pane_is_not_flagged():
    # Arrange — a pane we could not READ produced NO evidence. UNKNOWN is not
    # AUTH-FAILED, so an unread agent is never restarted (absence of evidence
    # is not evidence of a wedge).
    captures = {"gone": (None, None)}
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.auth_failed == ()


def test_only_the_frozen_agents_are_returned_and_sorted():
    # Arrange — a mixed fleet: two frozen, one transient, one clean.
    captures = {
        **stuck("zeta", "alpha"),
        **transient("mid"),
        "clean": (OK, OK),
    }
    # Act
    outcome = detect_login_expired(captures)
    # Assert — sorted, and ONLY the corroborated ones.
    assert outcome.auth_failed == ("alpha", "zeta")


# --- the third verdict SURVIVES: rejected is not the same as forgotten -----


def test_uncapturable_pane_is_reported_as_unknown():
    # Arrange — the same unread pane the test above proves is never restarted.
    # It must still come OUT of the detector, or the one agent we failed to
    # measure becomes the one agent nobody hears about.
    captures = {"gone": (None, None)}
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.unknown == ("gone",)


def test_clean_pane_is_reported_as_ok():
    # Arrange — positive evidence is a finding too: this is what lets a caller
    # say "we observed it and it was fine" instead of merely staying quiet.
    captures = {"writer": (OK, OK)}
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.ok == ("writer",)


def test_moving_banner_is_reported_as_ok_not_unknown():
    # Arrange — a banner that MOVED is a working agent, which we did observe.
    # It belongs in ok, not in the bucket reserved for what we never read.
    captures = transient("figrecipe")
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    assert outcome.ok == ("figrecipe",)


def test_every_agent_lands_in_exactly_one_bucket():
    # Arrange — the partition property: nothing handed to the detector may
    # vanish between the input and the three buckets, which is precisely how
    # an unread agent used to disappear.
    captures = {
        **stuck("zeta"),
        **transient("mid"),
        "clean": (OK, OK),
        "gone": (None, None),
    }
    # Act
    outcome = detect_login_expired(captures)
    # Assert
    buckets = outcome.auth_failed + outcome.ok + outcome.unknown
    assert sorted(buckets) == sorted(captures)


# --- the roster: an unreadable registry is not an empty one ----------------


def test_registry_lists_its_registered_agents(roster: Path):
    # Arrange — a REAL registry dir with real spec files, the same shape the
    # fleet-reconcile sweep enumerates.
    register_agents(roster, "scitex-hub", "writer")
    # Act
    outcome = registered_agents(roster)
    # Assert
    assert sorted(outcome.names) == ["scitex-hub", "writer"]


def test_empty_registry_is_readable_with_nobody_in_it(roster: Path):
    # Arrange — a registry that genuinely holds no agents. That is a real
    # answer, and it must stay distinguishable from having failed to ask.
    # Act
    outcome = registered_agents(roster)
    # Assert
    assert (outcome.readable, outcome.names) == (True, ())


def test_absent_registry_is_unreadable_not_empty(tmp_path: Path):
    # Arrange — the registry is not there, so we cannot know which agents
    # SHOULD have a live session. Reporting an empty roster here would assert
    # that nobody is missing, which is the strongest claim available and the
    # last one earned by having seen nothing.
    # Act
    outcome = registered_agents(tmp_path / "not-there")
    # Assert
    assert outcome.readable is False


def test_unreadable_registry_says_why(tmp_path: Path):
    # Arrange — the refusal has to travel with its reason, or a downstream exit
    # code is all that is left and nobody can act on it.
    # Act
    outcome = registered_agents(tmp_path / "not-there")
    # Assert
    assert "not-there" in outcome.detail
