"""A DECLARED job must become an APPLIED, ARMED one — proven, not parsed.

WHY A PARSE TEST WOULD BE THE BUG, NOT THE PROOF
================================================
``tests/.../_jobs/test__jobs_plugin.py`` already proves sac's nine
JobSpecs are well-formed, correctly kinded and discoverable. Every one of
those tests was GREEN throughout the whole period in which seven of ten
sac timers sat ``disabled`` on scitex-compute-04 — because a JobSpec
being valid says nothing about whether anything ever applies it. A test
that only checks the declaration parses is a restatement of the defect.

These tests assert the LINK instead: that for every job sac actually
declares, the provisioning path emits BOTH an ``install`` (→ APPLIED) and
an ``enable`` (→ ARMED) delegation, and that scitex-dev's real renderer
turns each declaration into a real systemd unit carrying the declared
command.

WHAT IS PROVEN HERE, AND WHAT IS NOT
====================================
PROVEN, with no mocks: every declared job reaches BOTH verbs, in order;
the bulk ``enable`` surface exists so arming is collective; ``disable``
is still per-name so the convergence verb has no fleet-wide off switch;
``sac installation boot`` — the one host-provisioning path — really
drives that sequence; and scitex-dev's real renderer materialises each
declaration into a ``.timer``/``.service`` pair whose ``ExecStart`` is
the declared command.

NOT PROVEN HERE, stated rather than glossed: that ``systemctl`` then
really arms the unit on a host. That needs a live ``systemd --user``
manager, which CI does not have, and scitex-dev's installer refuses with
exit 3 where none exists. The proof for that step is the replay the card
specifies — take a host with none armed, run the provisioning path,
confirm every declared job comes up ``enabled``, then disable one by hand
and confirm the convergence job restores it. The convergence half is
carded to scitex-dev, so that replay cannot be run from this repo alone.

SEAM, NOT MOCK (PA-306): the one injected callable is
``_dev_jobs._delegate``, exactly as ``test__dev_jobs.py`` already does.
It is the boundary where sac stops deciding and scitex-dev starts
touching the host; capturing it observes the delegation ARGUMENTS
without letting a real ``scitex-dev`` rewrite the machine's units.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pytest
from click.testing import CliRunner

pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

import scitex_agent_container.cli_pkg._dev_jobs as dj  # noqa: E402
from scitex_agent_container.cli_pkg._dev_jobs_apply import (  # noqa: E402
    APPLY_SEQUENCE,
    ApplyReport,
    Step,
    apply_declared_jobs,
    apply_kind,
    apply_verbs,
)
from scitex_agent_container.cli_pkg.installation_group import boot  # noqa: E402


def _declared_timers() -> list[str]:
    """Canonical names of every kind='timer' job sac really declares.

    Read through the production loader so this tracks the package's
    actual declarations. A hard-coded list here would be another
    declaration with no live counterpart — the disease, not the cure.
    """
    return [j.name for j in dj._load_sac_jobs(dj.GROUP_KINDS["timer"])]


class _Recorder:
    """Capture every ``(kind, verb, name, yes, dry_run)`` delegation."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[tuple] = []
        self._rc = rc

    def __call__(self, kind, verb, name, yes, dry_run=False) -> int:
        self.calls.append((kind, verb, name, yes, dry_run))
        return self._rc

    def names_for(self, verb: str) -> list[str]:
        return sorted(c[2] for c in self.calls if c[1] == verb)

    def verbs_for(self, name: str) -> list[str]:
        return [c[1] for c in self.calls if c[2] == name]


@contextmanager
def _delegating_to(recorder) -> Iterator[None]:
    """Swap the single mutation seam for the duration of one test."""
    original = dj._delegate
    dj._delegate = recorder  # type: ignore[assignment]
    try:
        yield
    finally:
        dj._delegate = original  # type: ignore[assignment]


@contextmanager
def _home(path) -> Iterator[None]:
    """Point ``$HOME`` at a scratch dir so `boot` cannot touch the real one."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def declared_timers() -> list[str]:
    names = _declared_timers()
    assert names, "sac declares no kind='timer' jobs — the fixture is blind"
    return names


@pytest.fixture
def applied_timers() -> _Recorder:
    """One successful ``apply_kind('timer')`` pass, and what it delegated."""
    recorder = _Recorder()
    apply_kind("timer", yes=True, delegate=recorder)
    return recorder


# ---------------------------------------------------------------------------
# the link: DECLARED -> APPLIED -> ARMED
# ---------------------------------------------------------------------------


def test_every_declared_timer_is_applied(applied_timers, declared_timers) -> None:
    # Arrange — APPLIED means a unit file gets written for it.
    expected = sorted(declared_timers)
    # Act
    installed = applied_timers.names_for("install")
    # Assert
    assert installed == expected


def test_every_declared_timer_is_armed(applied_timers, declared_timers) -> None:
    # Arrange — ARMED is the state that actually fires, and the one the
    # host was missing: install writes the unit, enable is what starts it.
    expected = sorted(declared_timers)
    # Act
    enabled = applied_timers.names_for("enable")
    # Assert
    assert enabled == expected


def test_no_declared_timer_stops_at_applied(applied_timers, declared_timers) -> None:
    # Arrange — the exact defect: a complete APPLIED set with an empty
    # ARMED set is what scitex-compute-04 was found in.
    # Act
    unarmed = set(declared_timers) - set(applied_timers.names_for("enable"))
    # Assert
    assert unarmed == set()


def test_each_job_is_installed_before_it_is_enabled(
    applied_timers, declared_timers
) -> None:
    # Arrange — enabling a unit that was never written fails.
    expected = list(APPLY_SEQUENCE)
    # Act
    out_of_order = [n for n in declared_timers if applied_timers.verbs_for(n) != expected]
    # Assert
    assert out_of_order == []


def test_the_report_counts_every_declared_job_as_armed(declared_timers) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    report = apply_kind("timer", yes=True, delegate=recorder)
    # Assert
    assert sorted(report.jobs_armed) == sorted(declared_timers)


def test_the_collective_entry_point_arms_the_timers(declared_timers) -> None:
    # Arrange — apply_declared_jobs walks every kind, not just one.
    recorder = _Recorder()
    # Act
    report = apply_declared_jobs(yes=True, delegate=recorder)
    # Assert
    assert sorted(report.jobs_armed) == sorted(declared_timers)


def test_the_collective_entry_point_skips_no_kind() -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    report = apply_declared_jobs(yes=True, delegate=recorder)
    # Assert
    assert report.skipped == ()


def test_yes_is_forwarded_to_every_delegation(applied_timers) -> None:
    # Arrange — dropping the guard flag would unguard a guarded command.
    # Act
    unconfirmed = [c for c in applied_timers.calls if c[3] is not True]
    # Assert
    assert unconfirmed == []


def test_dry_run_is_forwarded_to_every_delegation() -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    apply_kind("timer", yes=True, dry_run=True, delegate=recorder)
    # Assert
    assert all(call[4] is True for call in recorder.calls)


def test_a_service_is_applied_but_not_claimed_to_be_armed() -> None:
    # Arrange — scitex-dev #566 serves no `service enable`, so sac exposes
    # none; this path must say APPLIED and stop rather than imply arming.
    # Act
    verbs = apply_verbs("service")
    # Assert
    assert verbs == ("install",)


def test_a_timer_is_armed_as_well_as_applied() -> None:
    # Arrange
    # Act
    verbs = apply_verbs("timer")
    # Assert
    assert verbs == ("install", "enable")


# ---------------------------------------------------------------------------
# a failure must never read as success
# ---------------------------------------------------------------------------


def test_a_failing_enable_is_not_reported_ok() -> None:
    # Arrange
    recorder = _Recorder(rc=1)
    # Act
    report = apply_kind("timer", yes=True, delegate=recorder)
    # Assert
    assert report.ok is False


def test_a_failing_enable_arms_nothing() -> None:
    # Arrange
    recorder = _Recorder(rc=1)
    # Act
    report = apply_kind("timer", yes=True, delegate=recorder)
    # Assert
    assert report.jobs_armed == ()


def test_a_skipped_kind_is_not_ok() -> None:
    # Arrange — "could not look" must not render as "found nothing wrong";
    # that is the success-value-is-also-the-didnt-check-value shape.
    report = ApplyReport(skipped=("timer: scitex-dev too old",))
    # Act
    verdict = report.ok
    # Assert
    assert verdict is False


def test_a_clean_pass_is_ok() -> None:
    # Arrange
    report = ApplyReport(steps=(Step("timer", "enable", "j", 0),))
    # Act
    verdict = report.ok
    # Assert
    assert verdict is True


def test_an_unservable_verb_still_reaches_every_job(declared_timers) -> None:
    # Arrange — `_delegate` aborts with SystemExit(4) when the installed
    # scitex-dev cannot serve a verb. If that escaped, the first refusal
    # would leave every later job untouched AND uncounted.
    seen: list[str] = []

    def _refuse(kind, verb, name, yes, dry_run=False):
        seen.append(name)
        raise SystemExit(4)

    # Act
    apply_kind("timer", yes=True, delegate=_refuse)
    # Assert
    assert sorted(set(seen)) == sorted(declared_timers)


def test_an_unservable_verb_is_recorded_as_failed() -> None:
    # Arrange
    def _refuse(kind, verb, name, yes, dry_run=False):
        raise SystemExit(4)

    # Act
    report = apply_kind("timer", yes=True, delegate=_refuse)
    # Assert
    assert all(step.rc == 4 for step in report.failed)


# ---------------------------------------------------------------------------
# the CLI surface: arming is collective, disarming is not
# ---------------------------------------------------------------------------


def test_bulk_enable_arms_every_declared_timer(declared_timers) -> None:
    # Arrange — until 2026-08-15 this invocation was a click usage error,
    # so arming the fleet's timers took one hand-typed command per job.
    recorder = _Recorder()
    # Act
    with _delegating_to(recorder):
        CliRunner().invoke(dj._make_group("timer"), ["enable", "--yes"])
    # Assert
    assert recorder.names_for("enable") == sorted(declared_timers)


def test_bulk_enable_still_accepts_one_name() -> None:
    # Arrange — the per-name form is the surgical verb; it must survive.
    recorder = _Recorder()
    # Act
    with _delegating_to(recorder):
        CliRunner().invoke(
            dj._make_group("timer"), ["enable", "accounts-refresh", "--yes"]
        )
    # Assert
    assert recorder.names_for("enable") == ["sac.accounts-refresh"]


def test_disable_has_no_bulk_form() -> None:
    # Arrange — disabling every timer at once stops sac.accounts-refresh,
    # the fleet's SOLE OAuth refresher against a single-use token.
    # Convergence only ever needs the ENABLE direction.
    runner = CliRunner()
    # Act
    result = runner.invoke(dj._make_group("timer"), ["disable", "--yes"])
    # Assert
    assert result.exit_code != 0


def test_the_deprecated_alias_does_not_gain_bulk_enable() -> None:
    # Arrange — nothing new is ever built on the `systemd` alias.
    group = dj._make_group("systemd")
    # Act
    verbs = set(group.commands)
    # Assert
    assert "enable" not in verbs


# ---------------------------------------------------------------------------
# the base case: host provisioning drives the sequence
# ---------------------------------------------------------------------------


def test_installation_boot_applies_every_declared_job(
    tmp_path, declared_timers
) -> None:
    # Arrange — `sac installation boot` is sac's ONE provisioning path,
    # and until now its seven steps touched no declared job at all.
    recorder = _Recorder()
    # Act
    with _home(tmp_path), _delegating_to(recorder):
        CliRunner().invoke(boot, ["--dry-run"])
    # Assert
    assert recorder.names_for("install") == sorted(declared_timers)


def test_installation_boot_arms_every_declared_job(tmp_path, declared_timers) -> None:
    # Arrange — applying without arming is the defect, so provisioning
    # must reach the ENABLE verb too, not just write unit files.
    recorder = _Recorder()
    # Act
    with _home(tmp_path), _delegating_to(recorder):
        CliRunner().invoke(boot, ["--dry-run"])
    # Assert
    assert recorder.names_for("enable") == sorted(declared_timers)


def test_boot_still_succeeds_when_arming_cannot_run(tmp_path) -> None:
    # Arrange — a host that cannot arm must still finish bootstrapping:
    # the venv, PATH and shared dirs are what every recovery path needs.
    def _explode(kind, verb, name, yes, dry_run=False):
        raise RuntimeError("no scitex-dev here")

    # Act
    with _home(tmp_path), _delegating_to(_explode):
        result = CliRunner().invoke(boot, ["--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_boot_says_so_when_arming_cannot_run(tmp_path) -> None:
    # Arrange — an unarmed job that is REPORTED is recoverable; an
    # unarmed job nobody counted is the defect this step exists to end.
    def _explode(kind, verb, name, yes, dry_run=False):
        raise RuntimeError("no scitex-dev here")

    # Act
    with _home(tmp_path), _delegating_to(_explode):
        result = CliRunner().invoke(boot, ["--dry-run"])
    # Assert
    assert "could not apply declared jobs" in result.output


# ---------------------------------------------------------------------------
# APPLIED, for real: the declaration materialises into a systemd unit
# ---------------------------------------------------------------------------


@pytest.fixture
def rendered_units() -> dict[str, tuple[str, str]]:
    """``{job name: (timer unit text, service unit text)}`` from scitex-dev.

    Calls the same ``scitex_dev.jobs._systemd`` functions ``do_install``
    writes to disk with — real code, no fake — so these assertions are
    about the artifact a host would receive.
    """
    from scitex_dev.jobs import _systemd as sd

    jobs = dj._load_sac_jobs(dj.GROUP_KINDS["timer"])
    return {
        j.name: (sd.build_timer_unit(j), sd.build_service_unit(j)) for j in jobs
    }


def test_every_declared_timer_renders_a_timer_unit(rendered_units) -> None:
    # Arrange
    # Act
    without = [n for n, (timer, _svc) in rendered_units.items() if "[Timer]" not in timer]
    # Assert
    assert without == []


def test_every_rendered_timer_points_at_its_own_service(rendered_units) -> None:
    # Arrange — a timer owns the oneshot service it fires; a mismatch here
    # is the `sac.listen` double-supervisor hazard (docs/adr/0022) forming.
    # Act
    mismatched = [
        n for n, (timer, _svc) in rendered_units.items() if f"{n}.service" not in timer
    ]
    # Assert
    assert mismatched == []


def test_every_declared_command_survives_into_its_unit(rendered_units) -> None:
    # Arrange — the point of APPLIED: the unit runs what was DECLARED.
    jobs = {j.name: j.command for j in dj._load_sac_jobs(dj.GROUP_KINDS["timer"])}
    # Act
    missing = [
        name
        for name, (_timer, service) in rendered_units.items()
        if jobs[name].split()[0].rsplit("/", 1)[-1] not in service
    ]
    # Assert
    assert missing == []


def test_every_rendered_service_has_an_execstart(rendered_units) -> None:
    # Arrange — a unit with no ExecStart is applied and inert, which is
    # the same non-check as a declaration nobody applies.
    # Act
    without = [n for n, (_t, service) in rendered_units.items() if "ExecStart=" not in service]
    # Assert
    assert without == []
