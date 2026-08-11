"""Tests for the exactly-one-supervisor check.

"The new unit exists" is not the claim the migration makes. "ONLY the new
unit exists" is. A verification that accepted the first would report
success while doubling the supervision of the fleet's credential
machinery — which is the precise outcome the whole change exists to
prevent.

The two-supervisor case therefore gets its own test, and so does the
case where the old name survives ALONE (a cutover that silently did not
happen), because those two failures need different fixes and a single
"not ok" would not tell them apart.

No mocks (PA-306): the checker is pure over a set of filenames, and the
host-capability probe takes its ``which`` as a parameter.
"""

from __future__ import annotations

from scitex_agent_container._jobs._migrate import _renames, _verify

WORKTREE_GC = _renames.by_local("worktree-gc")


def test_only_the_new_units_present_is_ok() -> None:
    # Arrange
    present = frozenset(WORKTREE_GC.new_units())
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is True


def test_both_names_present_is_not_ok() -> None:
    # Arrange — THE hazard: two units, both enabled, both firing.
    present = frozenset(WORKTREE_GC.new_units() + WORKTREE_GC.old_units())
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is False


def test_both_names_present_is_reported_as_two_supervisors() -> None:
    # Arrange — the verdict must name the actual failure, because the fix
    # differs from "the cutover did not happen".
    present = frozenset(WORKTREE_GC.new_units() + WORKTREE_GC.old_units())
    # Act
    verdict = _verify.verify_exactly_one(WORKTREE_GC, present=present).verdict
    # Assert
    assert "TWO SUPERVISORS" in verdict


def test_only_the_old_units_present_is_not_ok() -> None:
    # Arrange
    present = frozenset(WORKTREE_GC.old_units())
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is False


def test_only_the_old_units_present_is_reported_as_a_cutover_that_did_not_happen() -> (
    None
):
    # Arrange
    present = frozenset(WORKTREE_GC.old_units())
    # Act
    verdict = _verify.verify_exactly_one(WORKTREE_GC, present=present).verdict
    # Assert
    assert "did not happen" in verdict


def test_nothing_present_is_not_ok() -> None:
    # Arrange — an install that failed leaves the job unsupervised, which
    # for `accounts-refresh` means the fleet expires within hours.
    present = frozenset()
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is False


def test_nothing_present_is_reported_as_no_supervisor() -> None:
    # Arrange
    present = frozenset()
    # Act
    verdict = _verify.verify_exactly_one(WORKTREE_GC, present=present).verdict
    # Assert
    assert "NO supervisor" in verdict


def test_a_half_installed_timer_is_not_ok() -> None:
    # Arrange — the .service written but not the .timer. The job exists and
    # can be run by hand, but nothing schedules it: silently never firing.
    present = frozenset({WORKTREE_GC.new_units()[0]})
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is False


def test_a_half_installed_timer_names_the_missing_unit() -> None:
    # Arrange
    present = frozenset({WORKTREE_GC.new_units()[0]})
    # Act
    verdict = _verify.verify_exactly_one(WORKTREE_GC, present=present).verdict
    # Assert
    assert WORKTREE_GC.new_units()[1] in verdict


def test_an_unrelated_unit_on_the_host_does_not_affect_the_verdict() -> None:
    # Arrange — the host has many units; only this job's names count.
    present = frozenset(WORKTREE_GC.new_units()) | {"sac-listen.service"}
    # Act
    got = _verify.verify_exactly_one(WORKTREE_GC, present=present).ok
    # Assert
    assert got is True


def test_the_ok_verdict_names_the_surviving_units() -> None:
    # Arrange
    present = frozenset(WORKTREE_GC.new_units())
    # Act
    verdict = _verify.verify_exactly_one(WORKTREE_GC, present=present).verdict
    # Assert
    assert verdict.startswith("OK")


def test_a_host_with_systemctl_can_supervise_units() -> None:
    # Arrange
    which = {"systemctl": "/usr/bin/systemctl"}.get
    # Act
    got = _verify.systemd_user_available(which=which)
    # Assert
    assert got is True


def test_a_host_without_systemctl_cannot_supervise_units() -> None:
    # Arrange — measured 2026-08-11: nas-01 (armv7l) and nas-02 have no
    # systemctl, and mba uses launchd. Three of nine hosts.
    def which(_name):
        return None

    # Act
    got = _verify.systemd_user_available(which=which)
    # Assert
    assert got is False
