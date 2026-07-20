"""The positional predicate, against the REAL pane both shipped detectors got wrong.

THE FIXTURE IS A PRODUCTION ARTEFACT, NOT A CONSTRUCTION
    ``specimen_grant_20260718_alive_false_positive.log`` is the operator's
    preserved capture of ``grant`` at 2026-07-18T22:26:19. grant was ALIVE at
    that moment — it answered a ping, read files, ran shell commands and
    completed a background publish task, at Context 86%.

    Both shipped detectors called it broken. This suite pins that: it asserts
    the new predicate returns ALIVE **and** demonstrates, on the same bytes,
    that the rules it replaces do not. A hand-built fixture could not do the
    second half honestly — it would encode my assumptions about the rendering
    rather than Anthropic's actual output — which is exactly why the real file
    is checked in.

WHAT IS NOT PROVEN HERE
    There is no captured TRUE POSITIVE (a banner rendered BELOW the startup
    marker). The DEAD branch is therefore exercised only against a synthetic
    pane, and is marked as such: it shows the predicate is not vacuous, and it
    does NOT establish that the branch fires correctly on production rendering.
    The restart arm stays disabled until a real one exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._authheal._positional import (
    ALIVE,
    DEAD,
    STARTUP_MARKER,
    UNKNOWN,
    classify_positional,
)
from scitex_agent_container._runners._tmux.auth_status import (
    evaluate,
    probe_pane,
    probe_to_state,
)

_SPECIMEN = (
    Path(__file__).parents[1]
    / "fixtures"
    / "pane_states"
    / "specimen_grant_20260718_alive_false_positive.log"
)


def _pane_section(text: str) -> str:
    """The pane-capture block of a specimen file, verbatim."""
    start = text.index("--- pane capture")
    start = text.index("\n", start) + 1
    return text[start : text.index("--- state.db row ---")]


@pytest.fixture
def grant_pane():
    return _pane_section(_SPECIMEN.read_text())


# ---------------------------------------------------------------------------
# THE REGRESSION: the real pane of a live agent must read ALIVE.
# ---------------------------------------------------------------------------


def test_real_specimen_of_a_working_agent_is_alive(grant_pane):
    # Arrange
    pane = grant_pane
    # Act
    verdict = classify_positional("grant", pane)
    # Assert
    assert verdict.state == ALIVE


def test_real_specimen_never_authorises_a_restart(grant_pane):
    # Arrange
    pane = grant_pane
    # Act
    verdict = classify_positional("grant", pane)
    # Assert
    assert verdict.may_restart is False


def test_real_specimen_banners_are_all_above_the_startup_marker(grant_pane):
    """The MECHANISM of the correct answer, not just the answer."""
    # Arrange
    pane = grant_pane
    # Act
    verdict = classify_positional("grant", pane)
    # Assert
    assert verdict.banners_below == ()


def test_real_specimen_really_does_contain_auth_banners(grant_pane):
    """Guard the fixture: ALIVE must be earned, not obtained by there being
    nothing to find. If the banners ever vanished from this file the regression
    would pass while testing nothing."""
    # Arrange
    pane = grant_pane
    # Act
    verdict = classify_positional("grant", pane)
    # Assert
    assert len(verdict.banner_lines) == 2


def test_real_specimen_really_does_contain_the_startup_marker(grant_pane):
    # Arrange
    pane = grant_pane
    # Act
    found = STARTUP_MARKER in pane
    # Assert
    assert found is True


# ---------------------------------------------------------------------------
# WHAT THE REPLACED RULES DO WITH THE SAME BYTES.
# ---------------------------------------------------------------------------


def test_sac_auth_status_rule_FALSELY_FLAGS_this_live_agent(grant_pane):
    """sac's near-prompt matcher puts the banner in the near-prompt tail.

    It is right about the geometry and wrong about the world: the banner IS the
    last non-chrome line above grant's prompt, and grant was working fine.
    """
    # Arrange
    pane = grant_pane
    # Act
    probe = probe_pane(pane)
    # Assert
    assert probe.present is True


def test_sac_auth_status_frozen_check_ALSO_calls_this_live_agent_stuck(grant_pane):
    """The freeze hardening does not rescue it — it confirms the error.

    An idle agent's pane does not change between captures, so re-reading the
    SAME pane is exactly what the frozen check sees in production for an idle
    agent, and it upgrades a false positive into a confident one.
    """
    # Arrange
    probe1, _ = evaluate(grant_pane, None)
    # Act
    _probe2, stuck = evaluate(grant_pane, probe_to_state(probe1))
    # Assert
    assert stuck is True


def test_bare_substring_rule_also_flags_this_live_agent(grant_pane):
    """The naive matcher auth-heal uses (banner anywhere on the visible screen)."""
    # Arrange
    pane = grant_pane
    # Act
    flagged = "Login expired" in pane
    # Assert
    assert flagged is True


# ---------------------------------------------------------------------------
# TRI-STATE. Only DEAD may ever act, and UNKNOWN never becomes a verdict.
# ---------------------------------------------------------------------------


def test_unreadable_pane_is_unknown():
    # Arrange
    pane = None
    # Act
    verdict = classify_positional("ghost", pane)
    # Assert
    assert verdict.state == UNKNOWN


def test_pane_without_the_startup_marker_is_unknown_not_dead():
    """Not finding our own anchor is a fact about our reading, not the agent."""
    # Arrange
    pane = "● Login expired · Please run /login\n❯\n"
    # Act
    verdict = classify_positional("unanchored", pane)
    # Assert
    assert verdict.state == UNKNOWN


def test_pane_without_the_startup_marker_still_reports_the_banners_it_ignored():
    """UNKNOWN must be auditable — say what was seen and set aside."""
    # Arrange
    pane = "● Login expired · Please run /login\n❯\n"
    # Act
    verdict = classify_positional("unanchored", pane)
    # Assert
    assert verdict.banner_lines == (0,)


def test_banner_below_the_marker_is_dead_SYNTHETIC_ONLY():
    """Shows the predicate is not vacuous. NOT evidence about production.

    No real pane with a banner below the startup marker has been captured, so
    this is a construction and is named to stop it being cited as a true
    positive. The restart arm stays disabled on that basis.
    """
    # Arrange
    pane = (
        "● Login expired · Please run /login\n"
        "❯ Start or continue. Scan your scitex-todo card slice\n"
        "● Login expired · Please run /login\n"
        "❯\n"
    )
    # Act
    verdict = classify_positional("synthetic", pane)
    # Assert
    assert verdict.state == DEAD


def test_even_a_dead_verdict_does_not_authorise_a_restart_yet():
    """The gate is in the type, not in a caller's discipline."""
    # Arrange
    pane = (
        "❯ Start or continue. Scan your scitex-todo card slice\n"
        "● Login expired · Please run /login\n"
    )
    # Act
    verdict = classify_positional("synthetic", pane)
    # Assert
    assert verdict.may_restart is False
