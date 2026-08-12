"""Tests for the migration table, checked against the REAL declared specs.

The warning that shaped this file: ``sac dev systemd list`` filtered on
``kind="systemd"`` — not a legal kind — matched nothing and printed "No sac
systemd-kind jobs." with exit 0 for WEEKS, hiding all four of sac's timers
including the OAuth refresher. The tests passed the whole time because the
fixture hand-rolled a fake whose ``_Job`` defaulted to ``kind="systemd"``,
a shape no real spec can have. A test double that accepts what the real
validator rejects is not a test.

So nothing here builds a fake spec. The coverage tests call the REAL
``provide_jobs()``, which constructs REAL ``JobSpec`` objects through the
REAL validator.

No mocks (PA-306). AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import re

import pytest

from scitex_agent_container._jobs import _names
from scitex_agent_container._jobs._jobs_plugin import provide_jobs
from scitex_agent_container._jobs._migrate import _renames

#: scitex-dev's PS-226 charset rule, restated because the auditor lives
#: behind a private module path. A name failing this derives a unit
#: filename with a dot in it.
PS226 = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _declared_names() -> frozenset[str]:
    return frozenset(j.name for j in provide_jobs())


def test_no_tabled_row_names_an_undeclared_job() -> None:
    # Arrange — a row's DECLARED name is `old` while it is held and `new`
    # once it has been cut over. A row naming neither is a row that
    # migrates something no provider offers.
    declared = _declared_names()
    # Act
    orphans = [
        r.local
        for r in _renames.RENAMES
        if (r.old if r.held else r.new) not in declared
    ]
    # Assert
    assert orphans == []


def test_every_declared_job_has_a_row() -> None:
    # Arrange — a job added without a row would never migrate.
    tabled = frozenset(r.new for r in _renames.RENAMES) | frozenset(
        r.old for r in _renames.RENAMES
    )
    # Act
    uncovered = _declared_names() - tabled
    # Assert
    assert uncovered == frozenset()


def test_a_held_job_still_declares_its_legacy_name() -> None:
    # Arrange — the invariant stopping the CLI from reporting a running
    # refresher as absent: a held spec must keep the name its unit has.
    held = [r for r in _renames.RENAMES if r.held]
    declared = _declared_names()
    # Act
    mismatched = [r.local for r in held if r.old not in declared]
    # Assert
    assert mismatched == []


def test_a_held_job_does_not_yet_declare_its_new_name() -> None:
    # Arrange
    held = [r for r in _renames.RENAMES if r.held]
    declared = _declared_names()
    # Act
    premature = [r.local for r in held if r.new in declared]
    # Assert
    assert premature == []


def test_every_tabled_kind_is_one_the_real_validator_accepts() -> None:
    # Arrange
    from scitex_dev.jobs import ALLOWED_KINDS

    # Act
    illegal = [r.new for r in _renames.RENAMES if r.kind not in ALLOWED_KINDS]
    # Assert
    assert illegal == []


def test_the_tabled_kind_matches_what_the_spec_declares() -> None:
    # Arrange
    by_name = {j.name: j for j in provide_jobs()}
    # Act
    wrong = [
        r.local
        for r in _renames.RENAMES
        if by_name[r.old if r.held else r.new].kind != r.kind
    ]
    # Assert
    assert wrong == []


@pytest.mark.parametrize("rename", _renames.RENAMES, ids=lambda r: r.new)
def test_every_new_name_is_hyphen_separated_lowercase(rename) -> None:
    # Arrange — PS-226 (error severity).
    name = rename.new
    # Act
    matched = PS226.match(name)
    # Assert
    assert matched is not None


@pytest.mark.parametrize("rename", _renames.RENAMES, ids=lambda r: r.new)
def test_every_new_name_is_package_qualified(rename) -> None:
    # Arrange — PS-227 expects `scitex-<pkg>-<name>`.
    name = rename.new
    # Act
    qualified = name.startswith("scitex-agent-container-")
    # Assert
    assert qualified is True


def test_the_old_names_are_exactly_what_ps226_rejects() -> None:
    # Arrange — a POSITIVE CONTROL. If the old names already passed the
    # rule, this whole migration would be a no-op wearing a migration's
    # clothes, and every other test here would pass anyway.
    olds = [r.old for r in _renames.RENAMES]
    # Act
    already_clean = [name for name in olds if PS226.match(name)]
    # Assert
    assert already_clean == []


def test_a_timer_job_materialises_two_units() -> None:
    # Arrange — displacing only the .timer leaves a runnable .service orphan.
    name = "x"
    # Act
    got = _renames.units_for(name, "timer")
    # Assert
    assert got == ("x.service", "x.timer")


def test_a_service_job_materialises_one_unit() -> None:
    # Arrange
    name = "x"
    # Act
    got = _renames.units_for(name, "service")
    # Assert
    assert got == ("x.service",)


def test_a_cron_job_materialises_no_units() -> None:
    # Arrange
    name = "x"
    # Act
    got = _renames.units_for(name, "cron")
    # Assert
    assert got == ()


def test_units_for_rejects_a_kind_the_validator_would_reject() -> None:
    # Arrange — "systemd" is a delivery mechanism, never a kind.
    # Act
    def _call():
        return _renames.units_for("x", "systemd")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_old_and_new_units_never_overlap() -> None:
    # Arrange — the premise of the whole migration: the rename really does
    # derive a different filename, so both could coexist.
    overlaps = [
        r.local
        for r in _renames.RENAMES
        if set(r.old_units()) & set(r.new_units())
    ]
    # Act
    count = len(overlaps)
    # Assert
    assert count == 0


def test_a_rename_that_renames_nothing_is_rejected() -> None:
    # Arrange
    # Act
    def _call():
        return _renames.Rename(old="a", new="a", kind="timer")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_a_hold_with_no_stated_reason_is_rejected() -> None:
    # Arrange — a hold with no reason is indistinguishable from an oversight.
    # Act
    def _call():
        return _renames.Rename(old="a", new="b", kind="timer", hold="   ")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_a_hold_with_a_reason_marks_the_row_held() -> None:
    # Arrange
    rename = _renames.Rename(old="a", new="b", kind="timer", hold="because X")
    # Act
    got = rename.held
    # Assert
    assert got is True


def test_a_row_with_no_hold_is_not_held() -> None:
    # Arrange
    rename = _renames.Rename(old="a", new="b", kind="timer")
    # Act
    got = rename.held
    # Assert
    assert got is False


def test_a_rename_rejects_a_kind_outside_the_taxonomy() -> None:
    # Arrange
    # Act
    def _call():
        return _renames.Rename(old="a", new="b", kind="systemd")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_by_local_finds_a_row_by_its_short_name() -> None:
    # Arrange
    typed = "worktree-gc"
    # Act
    got = _renames.by_local(typed).new
    # Assert
    assert got == "scitex-agent-container-worktree-gc"


def test_by_local_accepts_the_canonical_spelling() -> None:
    # Arrange — a name copied out of --json output must work as typed.
    typed = "scitex-agent-container-worktree-gc"
    # Act
    got = _renames.by_local(typed).local
    # Assert
    assert got == "worktree-gc"


def test_by_local_accepts_the_legacy_spelling() -> None:
    # Arrange — a name copied off an old unit filename must work too.
    typed = "sac.worktree-gc"
    # Act
    got = _renames.by_local(typed).local
    # Assert
    assert got == "worktree-gc"


def test_by_local_names_the_real_jobs_when_it_misses() -> None:
    # Arrange — a verb that silently does nothing for a typo is how a
    # scheduled job quietly stops being scheduled.
    # Act
    def _call():
        return _renames.by_local("wroktree-gc")

    # Assert
    with pytest.raises(KeyError, match="worktree-gc"):
        _call()


def test_the_hyphen_listen_unit_is_never_touchable() -> None:
    # Arrange
    unit = "sac-listen.service"
    # Act
    listed = unit in _renames.NEVER_TOUCH
    # Assert
    assert listed is True


def test_the_dotted_listen_unit_is_never_touchable() -> None:
    # Arrange — the name a JobSpec WOULD have derived; the near-miss
    # between the two is what once put two supervisors on port 7878.
    unit = "sac.listen.service"
    # Act
    listed = unit in _renames.NEVER_TOUCH
    # Assert
    assert listed is True


def test_no_tabled_row_names_a_never_touch_unit() -> None:
    # Arrange
    units = [u for r in _renames.RENAMES for u in r.old_units() + r.new_units()]
    # Act
    forbidden = [u for u in units if u in _renames.NEVER_TOUCH]
    # Assert
    assert forbidden == []


def test_local_names_round_trip_through_the_name_grammar() -> None:
    # Arrange
    rows = _renames.RENAMES
    # Act
    mismatched = [r.new for r in rows if _names.local(r.new) != r.local]
    # Assert
    assert mismatched == []
