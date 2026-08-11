"""Tests for the migration plan — above all, its ORDER.

Install-before-uninstall is the failure this module exists to make
impossible: a rename derives a DIFFERENT unit filename, so installing the
new unit before removing the old one leaves BOTH on the host, both
enabled, both firing the same command with no shared lock. On the head
node a crontab line and a systemd unit were already both supervising
``sac listen`` against different venvs, dormant only because a ``pgrep``
guard happened to match. A rename is exactly what wakes that up.

So the ordering assertions here are not style checks. They are the
countermeasure.

No mocks (PA-306): the planner is pure, and the host state it plans
against is passed in as plain sets of filenames.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._jobs._migrate import _plan, _renames

WORKTREE_GC = _renames.by_local("worktree-gc")

#: The host state as measured on compute-04 for one job: both units of a
#: timer present on disk.
BOTH_UNITS = frozenset(WORKTREE_GC.old_units())


def _actions(steps) -> list[str]:
    return [s.action for s in steps]


def test_a_plan_for_a_job_already_on_the_host_stops_it_first() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    # Act
    first = _actions(steps)[0]
    # Assert
    assert first == "stop"


def test_stop_precedes_disable_for_every_unit() -> None:
    # Arrange — disable only drops the .wants symlink, so a disable-first
    # order leaves a RUNNING unit nothing will restart and nothing stopped.
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    actions = _actions(steps)
    # Act
    ok = actions.index("stop") < actions.index("disable")
    # Assert
    assert ok is True


def test_displace_precedes_install() -> None:
    # Arrange — THE invariant. Two units both supervising one command is
    # the whole hazard.
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    actions = _actions(steps)
    # Act
    ok = actions.index("displace") < actions.index("install")
    # Assert
    assert ok is True


def test_every_step_rank_is_non_decreasing_for_every_tabled_job() -> None:
    # Arrange — the ordering is a property of the DATA: each action indexes
    # into ACTION_ORDER, so an edit that reorders the emission fails here
    # rather than quietly arming a race on nine hosts.
    present = frozenset(
        u for r in _renames.RENAMES for u in r.old_units()
    )
    offenders = []
    # Act
    for rename in _renames.RENAMES:
        ranks = [s.rank for s in _plan.plan_one(rename, present=present)]
        if ranks != sorted(ranks):
            offenders.append(rename.local)
    # Assert
    assert offenders == []


def test_carry_dropins_precedes_displace() -> None:
    # Arrange — displacing first orphans the drop-in under the old name,
    # where the new unit never reads it: the config silently stops applying
    # while everything still looks installed.
    dropins = frozenset({WORKTREE_GC.old_units()[0] + ".d"})
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS, dropin_dirs=dropins)
    actions = _actions(steps)
    # Act
    ok = actions.index("carry-dropins") < actions.index("displace")
    # Assert
    assert ok is True


def test_a_carry_step_names_the_new_dropin_directory_as_its_destination() -> None:
    # Arrange
    old_service = WORKTREE_GC.old_units()[0]
    dropins = frozenset({old_service + ".d"})
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS, dropin_dirs=dropins)
    # Act
    carried = [s for s in steps if s.action == "carry-dropins"]
    # Assert
    assert carried[0].dest == WORKTREE_GC.new_units()[0] + ".d"


def test_no_carry_step_is_planned_when_there_are_no_dropins() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS, dropin_dirs=frozenset())
    # Act
    carried = [s for s in steps if s.action == "carry-dropins"]
    # Assert
    assert carried == []


def test_a_held_job_plans_nothing() -> None:
    # Arrange — the refresher's cutover is operator-supervised; a bulk run
    # must not touch it at all.
    held = [r for r in _renames.RENAMES if r.held][0]
    # Act
    steps = _plan.plan_one(held, present=frozenset(held.old_units()))
    # Assert
    assert steps == ()


def test_a_job_absent_from_the_host_still_plans_an_install() -> None:
    # Arrange — the rename may have half-completed here.
    steps = _plan.plan_one(WORKTREE_GC, present=frozenset())
    # Act
    actions = _actions(steps)
    # Assert
    assert "install" in actions


def test_a_job_absent_from_the_host_plans_no_stop() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=frozenset())
    # Act
    actions = _actions(steps)
    # Assert
    assert "stop" not in actions


def test_a_job_absent_from_the_host_still_plans_a_verify() -> None:
    # Arrange — saying "no supervisor" out loud is what verify is for.
    steps = _plan.plan_one(WORKTREE_GC, present=frozenset())
    # Act
    actions = _actions(steps)
    # Assert
    assert actions[-1] == "verify"


def test_the_install_step_names_the_new_canonical_name() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    # Act
    install = [s for s in steps if s.action == "install"][0]
    # Assert
    assert WORKTREE_GC.new in install.argv


def test_the_default_install_delegates_rather_than_writing_the_unit() -> None:
    # Arrange — ownership returns to scitex-dev the moment the new name
    # exists; sac only retires the orphans it made.
    argv = _plan.default_install_argv(WORKTREE_GC)
    # Act
    exe = argv[0]
    # Assert
    assert exe == "scitex-dev"


def test_the_default_install_targets_a_group_the_installed_scitex_dev_has() -> None:
    # Arrange — the per-KIND groups arrive in scitex-dev #566; 0.47.0 is
    # installed here and serves only {cron, systemd} under `dev`.
    argv = _plan.default_install_argv(WORKTREE_GC)
    # Act
    path = argv[1:4]
    # Assert
    assert path == ("ecosystem", "dev", "systemd")


def test_a_logging_step_is_planned_for_the_service_unit() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    # Act
    logging_steps = [s for s in steps if s.action == "logging"]
    # Assert
    assert len(logging_steps) == 1


def test_the_logging_step_targets_a_dropin_not_the_unit_file() -> None:
    # Arrange — scitex-dev rewrites the unit on every install; anything
    # written into it is erased without a word.
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    # Act
    logging_step = [s for s in steps if s.action == "logging"][0]
    # Assert
    assert logging_step.target.endswith(".service.d/10-logging.conf")


def test_install_is_the_last_mutating_step_before_logging_and_verify() -> None:
    # Arrange
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    # Act
    tail = _actions(steps)[-3:]
    # Assert
    assert tail == ["install", "logging", "verify"]


def test_a_daemon_reload_separates_displacement_from_installation() -> None:
    # Arrange — so `verify` counts unit files that really exist.
    steps = _plan.plan_one(WORKTREE_GC, present=BOTH_UNITS)
    actions = _actions(steps)
    # Act
    ok = actions.index("displace") < actions.index("daemon-reload") < actions.index(
        "install"
    )
    # Assert
    assert ok is True


def test_the_full_plan_covers_every_unheld_job() -> None:
    # Arrange
    unheld = [r for r in _renames.RENAMES if not r.held]
    steps = _plan.plan(present=frozenset())
    # Act
    installed = {s.target for s in steps if s.action == "install"}
    # Assert
    assert installed == {r.new for r in unheld}


def test_the_full_plan_never_names_a_held_job() -> None:
    # Arrange
    held = [r for r in _renames.RENAMES if r.held]
    steps = _plan.plan(present=frozenset())
    # Act
    leaked = [s.target for s in steps if s.target in {r.new for r in held}]
    # Assert
    assert leaked == []


def test_a_step_must_state_its_evidence() -> None:
    # Arrange — a step that cannot say what was observed is a step nobody
    # can review, and this plan mutates credential machinery.
    # Act
    def _call():
        return _plan.Step(action="stop", target="x.timer", why="")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_a_step_with_an_unknown_action_is_rejected() -> None:
    # Arrange
    # Act
    def _call():
        return _plan.Step(action="frobnicate", target="x", why="because")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_a_plan_touching_the_listen_unit_is_refused() -> None:
    # Arrange — sac-listen.service supervises the fleet's control plane.
    forbidden = [
        _plan.Step(action="stop", target="sac-listen.service", why="should never happen")
    ]

    # Act
    def _call():
        return _plan.assert_never_touches_listen(forbidden)

    # Assert
    with pytest.raises(ValueError, match="NEVER touch"):
        _call()


def test_a_plan_touching_a_listen_dropin_is_refused() -> None:
    # Arrange — the 50-secrets-envrc.conf drop-in on compute-04.
    forbidden = [
        _plan.Step(
            action="carry-dropins",
            target="sac-listen.service.d/50-secrets-envrc.conf",
            why="should never happen",
        )
    ]

    # Act
    def _call():
        return _plan.assert_never_touches_listen(forbidden)

    # Assert
    with pytest.raises(ValueError, match="NEVER touch"):
        _call()


def test_the_real_plan_passes_the_listen_guard() -> None:
    # Arrange — plan() runs the guard on everything it returns, so this
    # would raise rather than return if a row ever named a listen unit.
    present = frozenset(u for r in _renames.RENAMES for u in r.old_units())
    # Act
    steps = _plan.plan(present=present)
    # Assert
    assert len(steps) > 0


def test_a_step_renders_its_argv_for_the_dry_run() -> None:
    # Arrange — the dry run is the review surface; a step that cannot print
    # what it would do is not reviewable.
    step = _plan.Step(
        action="stop", target="x.timer", why="because", argv=("systemctl", "stop", "x")
    )
    # Act
    rendered = step.render()
    # Assert
    assert "systemctl stop x" in rendered
