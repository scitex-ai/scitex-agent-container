"""Every nightly must be able to tell someone it failed.

WHAT THIS EXISTS TO PREVENT, measured 2026-08-12. ``main`` @5532e6b failed
``pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml`` on the nightly cron on Aug 5,
6, 7, 8, 9, 10 and 11 — every leg, **zero tests executed**, each time dying at
``mkdir: cannot create directory '/data': Permission denied``. Nobody was told,
because that workflow had three steps (run, upload coverage, clean scratch) and
no failure reporting anywhere in it.

A drift detector whose alarm does not exist is not a weak detector, it is a dead
one, and its silence is evidence about the detector rather than about the code.
A fleet survey the same night found 20 of 22 repos carrying this SIF shim had at
least one nightly whose latest run failed — so the silence was not this repo
being unlucky.

THE RULE THIS ENCODES is deliberately mechanical rather than a name check: any
workflow that runs the suite on a ``schedule`` must have a job that (a) depends
on the suite job and (b) can actually report — ``issues: write``. Adding a third
nightly without an alarm then turns this test RED instead of quietly recreating
the hole.

MUTATION CONTROLS. ``test_mutant_*`` strip the alarm out of an in-memory COPY of
the real workflow and assert the rule then reports a violation. A rule that
cannot be shown to fail proves nothing about the green.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"

# A workflow "runs the suite" if a step invokes one of these. Keyed on the
# scripts rather than on job or file names, because names drift and these are
# the actual entry points the nightly exercises.
_SUITE_SCRIPTS = ("run-in-sif.sh", "run-on-hosted.sh")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _triggers(wf: dict) -> dict:
    # `on` is YAML 1.1 truthy: PyYAML parses the bare key as True, not "on".
    trig = wf.get("on", wf.get(True))
    return trig if isinstance(trig, dict) else {}


def _is_scheduled(wf: dict) -> bool:
    return "schedule" in _triggers(wf)


def _suite_jobs(wf: dict) -> list[str]:
    found = []
    for job_id, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            if any(s in run for s in _SUITE_SCRIPTS):
                found.append(job_id)
                break
    return found


def _can_report(job: dict) -> bool:
    """A job that can actually raise an alarm, not merely one that is named one."""
    perms = job.get("permissions") or {}
    return isinstance(perms, dict) and perms.get("issues") == "write"


def alarm_violations(wf: dict) -> list[str]:
    """THE RULE. Empty list = this workflow can report its own failure."""
    if not _is_scheduled(wf):
        return []
    suite_jobs = _suite_jobs(wf)
    if not suite_jobs:
        return []
    jobs = wf.get("jobs") or {}
    for job_id, job in jobs.items():
        if job_id in suite_jobs or not _can_report(job):
            continue
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else list(needs or [])
        if any(n in suite_jobs for n in needs):
            return []
    return [
        f"no alarm job: nothing depends on {suite_jobs} with `issues: write`, "
        "so a failed nightly reports to nobody"
    ]


def _nightly_workflows() -> list[Path]:
    return sorted(
        p
        for p in list(_WORKFLOWS.glob("*.yml")) + list(_WORKFLOWS.glob("*.yaml"))
        if _is_scheduled(_load(p)) and _suite_jobs(_load(p))
    )


@pytest.fixture(scope="module")
def nightlies() -> list[Path]:
    return _nightly_workflows()


@pytest.fixture(scope="module")
def sif_nightly() -> dict:
    """The workflow that was silently red for seven nights."""
    matches = [p for p in _WORKFLOWS.glob("*.yml") if "run-in-sif.sh" in p.read_text()]
    gate = [p for p in matches if _is_scheduled(_load(p))]
    assert gate, "no scheduled workflow runs run-in-sif.sh — has the gate moved?"
    return _load(gate[0])


@pytest.fixture(scope="module")
def sif_alarm(sif_nightly) -> dict:
    return (sif_nightly.get("jobs") or {})["notify"]


# --------------------------------------------------------------------------
# The rule, over every nightly this repo has.
# --------------------------------------------------------------------------


def test_at_least_two_nightlies_are_policed(nightlies):
    """Guards the discovery itself: an empty sweep would pass every test below."""
    # Arrange
    found = nightlies
    # Act
    count = len(found)
    # Assert
    assert count >= 2, f"expected the hosted and SIF nightlies, found {found}"


def test_every_nightly_can_report_its_own_failure(nightlies):
    # Arrange
    offenders = {p.name: alarm_violations(_load(p)) for p in nightlies}
    # Act
    bad = {k: v for k, v in offenders.items() if v}
    # Assert
    assert not bad, f"nightly with no way to report a red night: {bad}"


# --------------------------------------------------------------------------
# The SIF nightly specifically — the one that was silent for seven nights.
# --------------------------------------------------------------------------


def test_sif_nightly_has_an_alarm_job(sif_nightly):
    # Arrange
    jobs = sif_nightly.get("jobs") or {}
    # Act
    present = "notify" in jobs
    # Assert
    assert present, f"the SIF nightly has no notify job: {list(jobs)}"


def test_sif_alarm_depends_on_the_suite(sif_alarm):
    # Arrange
    needs = sif_alarm.get("needs")
    # Act
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    # Assert
    assert "test" in needs, f"alarm does not observe the suite job: {needs}"


def test_sif_alarm_runs_on_a_schedule_not_only_on_pushes(sif_alarm):
    """A cron failure is the case with no human already looking at it."""
    # Arrange
    cond = " ".join(str(sif_alarm.get("if") or "").split())
    # Act
    covers_cron = "schedule" in cond
    # Assert
    assert covers_cron, f"alarm never fires for the nightly cron: {cond!r}"


def test_sif_alarm_also_runs_on_green_nights(sif_alarm):
    """`always()`, so a dead alarm is found on a quiet night rather than by the
    first regression that goes unreported. This is what makes silence mean green."""
    # Arrange
    cond = " ".join(str(sif_alarm.get("if") or "").split())
    # Act
    unconditional = cond.startswith("always()")
    # Assert
    assert unconditional, f"alarm only runs when already failing: {cond!r}"


def test_sif_alarm_does_not_share_fate_with_the_pool_it_watches(sif_alarm):
    """Every failure it announces is a failure OF the self-hosted pool; hosting
    the alarm there silences it in exactly the outages that matter most."""
    # Arrange
    runs_on = sif_alarm.get("runs-on")
    # Act
    hosted = isinstance(runs_on, str) and "self-hosted" not in runs_on
    # Assert
    assert hosted, f"alarm runs on the pool it reports on: {runs_on!r}"


def test_sif_alarm_can_actually_report(sif_alarm):
    # Arrange
    job = sif_alarm
    # Act
    reportable = _can_report(job)
    # Assert
    assert reportable, f"alarm lacks `issues: write`: {job.get('permissions')!r}"


def test_sif_alarm_aborts_on_a_failed_report(sif_alarm):
    """`set -e`. Without it a failing `gh issue comment` is masked by the next
    command's exit 0 and the step goes green having reported nothing — the
    original defect wearing a fix."""
    # Arrange
    script = "\n".join(str(s.get("run") or "") for s in sif_alarm["steps"])
    # Act
    strict = "set -euo pipefail" in script
    # Assert
    assert strict, "alarm script does not use `set -euo pipefail`"


def test_sif_alarm_verifies_the_issue_exists_afterwards(sif_alarm):
    """"the command exited 0" and "an open issue now exists" are different
    claims, and only the second one is the alarm having worked."""
    # Arrange
    script = "\n".join(str(s.get("run") or "") for s in sif_alarm["steps"])
    # Act
    checks = "--state open" in script and "ALARM FAILED SILENTLY" in script
    # Assert
    assert checks, "alarm never confirms an open issue exists after reporting"


def test_sif_nightly_has_a_selftest_input(sif_nightly):
    """The alarm had never fired in this workflow's life. A path that cannot be
    exercised on demand is indistinguishable from one that cannot fire."""
    # Arrange
    wd = _triggers(sif_nightly).get("workflow_dispatch") or {}
    # Act
    inputs = list((wd.get("inputs") or {}).keys())
    # Assert
    assert "selftest_force_red" in inputs, f"no way to rehearse the alarm: {inputs}"


def test_selftest_cannot_fire_on_an_ordinary_run(sif_nightly):
    """`inputs` is null off workflow_dispatch, so the guard must key on it alone
    — a condition that could be true on push would red the gate."""
    # Arrange
    steps = (sif_nightly["jobs"]["test"]).get("steps") or []
    # Act
    guards = [
        " ".join(str(s.get("if") or "").split())
        for s in steps
        if "selftest" in str(s.get("run") or "").lower()
        or "selftest" in " ".join(str(s.get("if") or "").split())
    ]
    # Assert
    assert guards and all("inputs.selftest_force_red" in g for g in guards), (
        f"self-test step is not gated on the dispatch input alone: {guards}"
    )


# --------------------------------------------------------------------------
# Mutation controls: remove the alarm and prove the rule notices.
# --------------------------------------------------------------------------


def test_mutant_nightly_without_any_alarm_is_reported(sif_nightly):
    # Arrange
    mutant = copy.deepcopy(sif_nightly)
    # Act
    del mutant["jobs"]["notify"]
    # Assert
    assert alarm_violations(mutant), (
        "removing the alarm entirely produced NO violation — the rule is not "
        "what makes the passing tests above pass"
    )


def test_mutant_alarm_without_report_permission_is_reported(sif_nightly):
    """A job named `notify` that cannot open an issue is decoration."""
    # Arrange
    mutant = copy.deepcopy(sif_nightly)
    # Act
    mutant["jobs"]["notify"]["permissions"] = {"contents": "read"}
    # Assert
    assert alarm_violations(mutant), (
        "an alarm stripped of `issues: write` was still accepted"
    )


def test_mutant_alarm_not_wired_to_the_suite_is_reported(sif_nightly):
    """An alarm that does not depend on the suite never learns it failed."""
    # Arrange
    mutant = copy.deepcopy(sif_nightly)
    # Act
    mutant["jobs"]["notify"]["needs"] = []
    # Assert
    assert alarm_violations(mutant), "an alarm observing nothing was still accepted"


def test_rule_is_silent_for_workflows_that_do_not_run_the_suite():
    """Scope control: autobump and auto-merge are scheduled too, and demanding
    an alarm of them would make this rule noise."""
    # Arrange
    wf = {True: {"schedule": [{"cron": "0 17 * * *"}]}, "jobs": {"x": {"steps": []}}}
    # Act
    violations = alarm_violations(wf)
    # Assert
    assert not violations, f"rule fired on a non-suite scheduled workflow: {violations}"
