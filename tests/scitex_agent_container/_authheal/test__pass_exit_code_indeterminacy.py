"""A supervisor that can never go green is a supervisor nobody reads.

MEASURED 2026-08-05 on the fleet host: `sac agents restart-login-expired`
returned 2 on every pass, with UNOBSERVED=92 and all 92 carrying the reason
``no-session``. Not one was wedged. The roster is spec FILES — 231 of them on
this host — while the number of agents actually running is a fraction of that,
BY DESIGN. So every pass carried sessionless reports, and exit 0 was unreachable
for every possible state of the fleet, healthy or not.

That is the shape the constitution names: a gate configured so it cannot fail is
the same as deleting it. This is its twin — a gate that cannot PASS. Both end
with a signal nobody reads, and the second is worse for being loud.

These tests pin the discrimination, in both directions, because a fix that just
made 0 reachable would trade a useless red for a dangerous green.
"""

from __future__ import annotations

from scitex_agent_container._authheal._pass import AgentReport, PassOutcome
from scitex_agent_container._reconcile._rule import Verdict


def _sessionless(name: str) -> AgentReport:
    """A registered agent with no tui- session — the 92-of-92 case."""
    return AgentReport(name, Verdict.UNOBSERVED, "no-session", f"{name}: no session")


def _unreadable(name: str) -> AgentReport:
    """A LIVE session whose pane would not capture — a real blind spot."""
    return AgentReport(
        name, Verdict.UNOBSERVED, "pane-unreadable", f"{name}: pane read failed"
    )


def _healthy(name: str) -> AgentReport:
    """An agent that WAS read and is fine."""
    return AgentReport(name, Verdict.OK, "no-login-prompt", f"{name}: fine")


def test_a_fleet_of_sessionless_agents_and_no_wedge_is_clean() -> None:
    """The measured reality: 92 sessionless, 0 wedged. This must be 0."""
    # Arrange
    outcome = PassOutcome(reports=tuple(_sessionless(f"a{i}") for i in range(92)))
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 0


def test_one_observed_agent_among_the_sessionless_is_still_clean() -> None:
    """The realistic shape — a few running, most registered and idle."""
    # Arrange
    reports = tuple(_sessionless(f"a{i}") for i in range(91)) + (_healthy("live"),)
    outcome = PassOutcome(reports=reports)
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 0


def test_a_pane_we_could_not_read_is_still_indeterminate() -> None:
    """This one IS us failing to look, and it must keep outranking clean."""
    # Arrange
    reports = tuple(_sessionless(f"a{i}") for i in range(91)) + (_unreadable("live"),)
    outcome = PassOutcome(reports=reports)
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 2


def test_an_unreadable_roster_is_still_indeterminate() -> None:
    """Not knowing WHO should be running defeats any claim about all of them."""
    # Arrange
    report = AgentReport(
        "<fleet-roster>", Verdict.UNOBSERVED, "roster-unreadable", "specs dir gone"
    )
    outcome = PassOutcome(reports=(report,))
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 2


def test_an_unknown_budget_is_still_indeterminate() -> None:
    """The other half of exit 2 must not be disturbed by this change."""
    # Arrange
    report = AgentReport("a1", Verdict.BUDGET_UNKNOWN, "history-unreadable", "no file")
    outcome = PassOutcome(reports=(report,))
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 2


def test_a_still_wedged_agent_outranks_a_field_of_sessionless_ones() -> None:
    """The thing this supervisor exists for must still reach the exit code.

    FAILED, not RESTARTED: a restart that WORKED leaves nothing wedged, so 0 is
    the right answer there. Writing this test with RESTARTED is how I found out
    I had the two confused — the exit code was right and my premise was not.
    """
    # Arrange
    wedged = AgentReport("w", Verdict.FAILED, "restart-returned-false", "w: still")
    outcome = PassOutcome(
        reports=tuple(_sessionless(f"a{i}") for i in range(91)) + (wedged,)
    )
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 1


def test_a_successful_restart_among_sessionless_agents_is_clean() -> None:
    """A wedge that was FIXED is not a wedge — 0 is the honest answer."""
    # Arrange
    fixed = AgentReport("w", Verdict.RESTARTED, "login-expired", "w: restarted")
    outcome = PassOutcome(
        reports=tuple(_sessionless(f"a{i}") for i in range(91)) + (fixed,)
    )
    # Act
    code = outcome.exit_code()
    # Assert
    assert code == 0


def test_sessionless_reports_are_not_indeterminate() -> None:
    """The predicate itself, so a failure names the cause not the symptom."""
    # Arrange
    outcome = PassOutcome(reports=(_sessionless("a1"), _sessionless("a2")))
    # Act
    unresolved = outcome.indeterminate()
    # Assert
    assert unresolved == ()


def test_the_sessionless_count_survives_a_clean_exit() -> None:
    """Demoted from the exit code, NOT hidden — a reader must still see 92."""
    # Arrange
    outcome = PassOutcome(reports=tuple(_sessionless(f"a{i}") for i in range(92)))
    # Act
    counted = outcome.counts()
    # Assert
    assert counted[Verdict.UNOBSERVED.value] == 92


# --- the RENDERED populations ------------------------------------------
# exit_code() and the printed summary must be driven by the SAME split, or the
# command contradicts itself. Measured 2026-08-16 on this host: a pass exited 0
# while printing "this pass therefore CANNOT report a clean fleet" about 100
# sessionless agents. The verdict had been fixed months earlier; the narration
# still counted EVERY UNOBSERVED. These pin the two populations the CLI renders
# separately, so the alarming banner can never again describe a clean pass.


def test_sessionless_reports_are_not_this_passs_indeterminacy() -> None:
    """The banner population must be empty when nothing was truly unreadable."""
    # Arrange
    outcome = PassOutcome(reports=tuple(_sessionless(f"a{i}") for i in range(100)))
    # Act
    indeterminate = outcome.indeterminate()
    # Assert
    assert indeterminate == ()


def test_an_unreadable_pane_is_this_passs_indeterminacy() -> None:
    """A live-but-unreadable pane is the real blind spot and must be named."""
    # Arrange
    outcome = PassOutcome(reports=(_sessionless("idle"), _unreadable("live")))
    # Act
    names = tuple(r.name for r in outcome.indeterminate())
    # Assert
    assert names == ("live",)


def test_the_two_rendered_populations_partition_every_unobserved() -> None:
    """Exhaustive AND disjoint: no report may vanish, none may be printed twice.

    Comparing the concatenation to the whole pins both at once — an overlap
    makes it longer, a gap makes it shorter.
    """
    # Arrange
    outcome = PassOutcome(
        reports=(_sessionless("idle1"), _unreadable("live1"), _sessionless("idle2"))
    )
    unseen = outcome.of(Verdict.UNOBSERVED)
    # Act
    rendered = outcome.indeterminate() + tuple(
        r for r in unseen if r.reason == "no-session"
    )
    # Assert
    assert sorted(r.name for r in rendered) == sorted(r.name for r in unseen)
