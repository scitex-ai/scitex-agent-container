"""The READ-ONLY detector: which live agents are CORROBORATED login-expired.

``detect_login_expired`` is the gate in front of every restart, so its whole
job is to be conservative: it fires ONLY on a banner that is frozen across
the two captures (the real ``evaluate_agents`` matcher runs here, unmocked),
and it must reject a banner that moved, a clean pane, and an unreadable pane.
A false positive here is a needless restart that destroys a working agent's
context, so these are the tests that keep the auto-restarter safe.
"""

from __future__ import annotations

from scitex_agent_container._authheal._detect import detect_login_expired

from ._helpers import OK, stuck, transient


def test_frozen_banner_is_corroborated_login_expired():
    # Arrange — a banner identical on both reads = frozen = the real thing.
    captures = stuck("scitex-hpc")
    # Act
    names = detect_login_expired(captures)
    # Assert
    assert names == ["scitex-hpc"]


def test_single_run_transient_banner_is_not_flagged():
    # Arrange — a banner on run 1 that is GONE on the decisive run 2. The
    # agent is producing output (working or merely quoting the incident), so
    # it is NOT wedged and must never be restarted.
    captures = transient("figrecipe")
    # Act
    names = detect_login_expired(captures)
    # Assert
    assert names == []


def test_clean_pane_is_not_flagged():
    # Arrange — no banner at all on either read.
    captures = {"writer": (OK, OK)}
    # Act
    names = detect_login_expired(captures)
    # Assert
    assert names == []


def test_uncapturable_pane_is_not_flagged():
    # Arrange — a pane we could not READ produced NO evidence. UNKNOWN is not
    # AUTH-FAILED, so an unread agent is never restarted (absence of evidence
    # is not evidence of a wedge).
    captures = {"gone": (None, None)}
    # Act
    names = detect_login_expired(captures)
    # Assert
    assert names == []


def test_only_the_frozen_agents_are_returned_and_sorted():
    # Arrange — a mixed fleet: two frozen, one transient, one clean.
    captures = {
        **stuck("zeta", "alpha"),
        **transient("mid"),
        "clean": (OK, OK),
    }
    # Act
    names = detect_login_expired(captures)
    # Assert — sorted, and ONLY the corroborated ones.
    assert names == ["alpha", "zeta"]
