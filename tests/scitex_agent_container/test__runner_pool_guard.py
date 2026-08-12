"""Tests for the no-hardcoded-runner-pool guard.

A GUARD YOU HAVE ONLY EVER SEEN PASS IS A HOPE. The guard this one sits
beside was written after exactly that lesson, and this file inherits it:
every test below either proves the guard FIRES on a real frozen pin, or
proves it does NOT cry wolf on a spelling we actually use.

The decisive one is :func:`test_reintroducing_the_incident_pin_is_caught`.
It takes THIS repo's real ``lint.yml``, puts back the literal
``["self-hosted","Linux","X64","spartan-cpu"]`` that PR #1006 shipped and
that went on to jam fifteen PRs, and asserts the guard reports it. Without
that test the guard is calibrated to a story rather than to the incident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._runner_pool_guard import (
    CODE_ALLOWLIST_NO_REASON,
    CODE_ALLOWLIST_STALE,
    CODE_FROZEN_POOL,
    check_repo,
    load_allowlist,
    main,
    pool_labels,
    reads_a_variable,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The seam every gated job in this repo uses.
CANONICAL = (
    '${{ fromJSON(vars.CI_RUNS_ON || \'["self-hosted","Linux","X64","scitex-ci"]\') }}'
)

# The light lane's seam: its own variable, falling through to the main one.
LIGHT = (
    "${{ fromJSON(vars.LIGHT_RUNS_ON || vars.CI_RUNS_ON || "
    "'[\"self-hosted\",\"Linux\",\"X64\",\"scitex-ci\"]') }}"
)

# The exact literal PR #1006 shipped, and the exact literal that jammed the
# queue when those runners went offline.
INCIDENT_PIN = '["self-hosted", "Linux", "X64", "spartan-cpu"]'

# The five jobs the incident jammed. Listed one by one rather than derived,
# so DELETING a seam cannot quietly shrink the test.
LIGHT_LANE = [
    ("lint.yml", "ruff"),
    ("import-smoke-on-ubuntu-py3-12.yml", "install-check"),
    ("no-hosted-runners-guard-on-self-hosted.yml", "no-hosted-runners"),
    ("rtd-sphinx-build-on-ubuntu-latest.yml", "sphinx"),
    ("quality-audit-on-ubuntu-latest.yml", "audit"),
]

# The two pins this repo keeps on purpose, with their arguments on file.
ALLOWED_PINS = [
    ("spartan-capacity-canary-on-self-hosted.yml", "canary"),
    ("pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml", "verdict"),
]

GOOD_REASON = (
    "This job must reach one named machine because it writes to that host's "
    "database over loopback; a pool would deliver the write to nobody."
)

REUSABLE_CALLER = (
    "name: demo\non: [push]\njobs:\n  call:\n    uses: ./.github/workflows/other.yml\n"
)


def _workflow(runs_on: str, job_id: str = "build") -> str:
    return (
        "name: demo\n"
        "on: [push]\n"
        "jobs:\n"
        f"  {job_id}:\n"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )


def _two_pinned_jobs() -> str:
    return (
        "name: demo\n"
        "on: [push]\n"
        "jobs:\n"
        "  canary:\n"
        f"    runs-on: {INCIDENT_PIN}\n"
        "    steps:\n"
        "      - run: echo hi\n"
        "  sneaky:\n"
        f"    runs-on: {INCIDENT_PIN}\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )


def _write_repo(
    root: Path,
    workflows: dict[str, str],
    allowlist: str | None = None,
) -> Path:
    """Materialise a throwaway repo with real files on disk (no mocks)."""
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    for name, body in workflows.items():
        (wf_dir / name).write_text(body, encoding="utf-8")
    if allowlist is not None:
        (root / ".github" / "runner-pin-allowlist.yaml").write_text(
            allowlist, encoding="utf-8"
        )
    return root


def _allowlist(workflow: str, jobs: str = "", reason: str = GOOD_REASON) -> str:
    jobs_line = f"    jobs: [{jobs}]\n" if jobs else ""
    return (
        f"allow:\n  - workflow: {workflow}\n{jobs_line}    reason: >-\n      {reason}\n"
    )


def _codes(root: Path) -> list[str]:
    return [violation.code for violation in check_repo(root)]


def _job(filename: str, job_id: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
    return doc["jobs"][job_id]


@pytest.fixture
def regressed_repo(tmp_path: Path) -> Path:
    """THIS repo's real ``lint.yml`` with the incident pin put back.

    Not a fixture that resembles the incident — the bytes this repo shipped
    on 2026-08-06, and the bytes that stopped five checks from ever starting
    on 2026-08-12. ``pytest.fail`` rather than ``assert`` if the substitution
    is a no-op: a silently-unchanged file would make every test below pass
    against a clean tree and prove nothing at all.
    """
    live = (WORKFLOWS / "lint.yml").read_text(encoding="utf-8")
    regressed = live.replace(f"runs-on: {LIGHT}", f"runs-on: {INCIDENT_PIN}")
    if regressed == live:
        pytest.fail("the light-lane seam moved — update LIGHT in this test module")
    return _write_repo(tmp_path, {"lint.yml": regressed})


# ───────────────────────────────────────────────────────────────────────────
# FAIL DIRECTION — the guard must actually fire.
# ───────────────────────────────────────────────────────────────────────────


def test_reintroducing_the_incident_pin_is_caught(regressed_repo: Path) -> None:
    # Arrange: fixture rewrote the real lint.yml back to the #1006 pin
    repo = regressed_repo
    # Act
    codes = _codes(repo)
    # Assert
    assert codes == [CODE_FROZEN_POOL]


def test_reintroducing_the_incident_pin_names_the_dead_pool(
    regressed_repo: Path,
) -> None:
    # Arrange
    repo = regressed_repo
    # Act
    violations = check_repo(repo)
    # Assert
    assert "spartan-cpu" in violations[0].message


def test_reintroducing_the_incident_pin_exits_non_zero(regressed_repo: Path) -> None:
    # Arrange
    repo = regressed_repo
    # Act
    exit_code = main([str(repo)])
    # Assert
    assert exit_code == 1


@pytest.mark.parametrize(
    "runs_on",
    [
        '["self-hosted", "Linux", "X64", "spartan-cpu"]',  # JSON-ish flow list
        "[self-hosted, Linux, X64, scitex-org-cpu]",  # bare YAML flow list
        "[self-hosted, scitex-ci]",  # the old default, frozen
        "scitex-ci",  # a bare scalar label
        "{labels: [self-hosted, Linux, spartan-cpu]}",  # the mapping form
    ],
)
def test_every_frozen_spelling_is_flagged(tmp_path: Path, runs_on: str) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(runs_on)})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_FROZEN_POOL]


def test_the_violation_names_the_frozen_pool(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(INCIDENT_PIN)})
    # Act
    message = check_repo(tmp_path)[0].message
    # Assert
    assert "spartan-cpu" in message


def test_the_violation_shows_the_seam_to_write_instead(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(INCIDENT_PIN)})
    # Act
    message = check_repo(tmp_path)[0].message
    # Assert
    assert "vars.CI_RUNS_ON" in message


def test_a_second_job_in_an_allowlisted_file_does_not_ride_along(
    tmp_path: Path,
) -> None:
    # Arrange: the entry covers `canary` only
    _write_repo(
        tmp_path,
        {"canary.yml": _two_pinned_jobs()},
        allowlist=_allowlist("canary.yml", jobs="canary"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_FROZEN_POOL]


def test_a_second_job_in_an_allowlisted_file_is_named(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"canary.yml": _two_pinned_jobs()},
        allowlist=_allowlist("canary.yml", jobs="canary"),
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert "sneaky" in violations[0].where


def test_allowlist_entry_without_reason_is_rejected(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"canary.yml": _workflow(INCIDENT_PIN, job_id="canary")},
        allowlist="allow:\n  - workflow: canary.yml\n",
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_NO_REASON in codes


def test_allowlist_entry_without_reason_does_not_exempt_the_job(
    tmp_path: Path,
) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"canary.yml": _workflow(INCIDENT_PIN, job_id="canary")},
        allowlist="allow:\n  - workflow: canary.yml\n",
    )
    # Act: a rejected entry must not smuggle the pin through
    codes = _codes(tmp_path)
    # Assert
    assert CODE_FROZEN_POOL in codes


def test_allowlist_entry_with_stub_reason_is_rejected(tmp_path: Path) -> None:
    # Arrange: a shrug ("legacy") is not an argument
    _write_repo(
        tmp_path,
        {"canary.yml": _workflow(INCIDENT_PIN, job_id="canary")},
        allowlist='allow:\n  - workflow: canary.yml\n    reason: "legacy"\n',
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_NO_REASON in codes


def test_stale_allowlist_entry_is_flagged(tmp_path: Path) -> None:
    # Arrange: ci.yml reads the variable, so it needs no exception
    _write_repo(
        tmp_path,
        {"ci.yml": _workflow(CANONICAL)},
        allowlist=_allowlist("ci.yml"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_ALLOWLIST_STALE]


def test_allowlist_entry_for_missing_workflow_is_flagged(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"ci.yml": _workflow(CANONICAL)},
        allowlist=_allowlist("gone.yml"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_ALLOWLIST_STALE]


# ───────────────────────────────────────────────────────────────────────────
# PASS DIRECTION — the guard must not cry wolf.
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("runs_on", [CANONICAL, LIGHT])
def test_the_variable_seams_pass(tmp_path: Path, runs_on: str) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(runs_on)})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_generic_labels_alone_are_not_a_pin(tmp_path: Path) -> None:
    # Arrange: `[self-hosted, Linux, X64]` selects no POOL — it reaches
    # whichever of our runners is free, which is the opposite of a pin.
    _write_repo(tmp_path, {"ci.yml": _workflow("[self-hosted, Linux, X64]")})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_a_hosted_job_is_left_to_the_other_guard(tmp_path: Path) -> None:
    # Arrange: `ubuntu-latest` is SAC-CI001's business. Reporting it here too
    # would have the two guards argue about one line in different words.
    _write_repo(tmp_path, {"cla.yml": _workflow("ubuntu-latest")})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_reusable_workflow_call_is_not_flagged(tmp_path: Path) -> None:
    # Arrange: a `uses:` job declares no runner of its own
    _write_repo(
        tmp_path,
        {
            "caller.yml": REUSABLE_CALLER,
            "other.yml": _workflow(CANONICAL, job_id="inner"),
        },
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_allowlisted_pin_passes(tmp_path: Path) -> None:
    # Arrange: the approved pin, with its argument attached
    _write_repo(
        tmp_path,
        {"canary.yml": _workflow(INCIDENT_PIN, job_id="canary")},
        allowlist=_allowlist("canary.yml", jobs="canary"),
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_allowlisted_pin_exits_zero(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"canary.yml": _workflow(INCIDENT_PIN, job_id="canary")},
        allowlist=_allowlist("canary.yml", jobs="canary"),
    )
    # Act
    exit_code = main([str(tmp_path)])
    # Assert
    assert exit_code == 0


# ───────────────────────────────────────────────────────────────────────────
# The two predicates, directly.
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "runs_on,expected",
    [
        (["self-hosted", "Linux", "X64"], []),
        (["self-hosted", "Linux", "X64", "spartan-cpu"], ["spartan-cpu"]),
        # Case must not launder a pin.
        (["Self-Hosted", "LINUX", "Scitex-CI"], ["Scitex-CI"]),
        ({"labels": ["self-hosted", "sac-control-plane"]}, ["sac-control-plane"]),
    ],
)
def test_pool_labels_ignores_only_githubs_automatic_labels(runs_on, expected) -> None:
    # Arrange
    job = {"runs-on": runs_on}
    # Act
    pools = pool_labels(job)
    # Assert
    assert pools == expected


@pytest.mark.parametrize(
    "runs_on,expected",
    [
        (CANONICAL, True),
        (LIGHT, True),
        ("${{ vars.ANYTHING }}", True),
        (["self-hosted", "spartan-cpu"], False),
        ("scitex-ci", False),
    ],
)
def test_reads_a_variable_sees_the_seam(runs_on, expected: bool) -> None:
    # Arrange
    job = {"runs-on": runs_on}
    # Act
    seam = reads_a_variable(job)
    # Assert
    assert seam is expected


# ───────────────────────────────────────────────────────────────────────────
# THE REAL REPO.
# ───────────────────────────────────────────────────────────────────────────


def test_this_repo_is_clean() -> None:
    # Arrange
    repo = REPO_ROOT
    # Act
    violations = check_repo(repo)
    # Assert
    assert violations == [], "\n".join(v.render() for v in violations)


@pytest.mark.parametrize("filename,job_id", LIGHT_LANE)
def test_no_light_lane_job_names_a_pool_literally(filename: str, job_id: str) -> None:
    # Arrange
    job = _job(filename, job_id)
    # Act
    seam = reads_a_variable(job)
    # Assert
    assert seam, f"{filename} -> {job_id} froze its pool again"


@pytest.mark.parametrize("filename,job_id", LIGHT_LANE)
def test_the_light_lane_falls_back_to_the_main_pool_variable(
    filename: str, job_id: str
) -> None:
    # Arrange: LIGHT_RUNS_ON is deliberately UNSET in repo settings, so the
    # `|| vars.CI_RUNS_ON` fall-through is what actually routes these jobs
    # today. A light lane reading ONLY its own variable would be dark.
    text = (WORKFLOWS / filename).read_text(encoding="utf-8")
    # Act
    has_fallthrough = "vars.LIGHT_RUNS_ON || vars.CI_RUNS_ON" in text
    # Assert
    assert has_fallthrough, f"{filename} -> {job_id} lost its fall-through"


def test_the_repo_pin_allowlist_is_fully_argued() -> None:
    # Arrange
    repo = REPO_ROOT
    # Act
    _, allow_violations = load_allowlist(repo)
    # Assert
    assert allow_violations == []


@pytest.mark.parametrize("filename,job_id", ALLOWED_PINS)
def test_each_repo_pin_exception_is_on_file(filename: str, job_id: str) -> None:
    # Arrange
    repo = REPO_ROOT
    # Act
    allow, _ = load_allowlist(repo)
    # Assert
    assert filename in allow, f"the argued exception for {job_id} vanished"


@pytest.mark.parametrize("filename,job_id", ALLOWED_PINS)
def test_each_allowlisted_pin_really_still_pins_a_pool(
    filename: str, job_id: str
) -> None:
    # Arrange: an exception must be LOAD-BEARING, not decorative — a stale
    # entry is SAC-CI007, and this names which one died.
    job = _job(filename, job_id)
    # Act
    pools = pool_labels(job)
    # Assert
    assert pools, f"{filename} -> {job_id} no longer pins a pool"


@pytest.mark.parametrize("filename,job_id", ALLOWED_PINS)
def test_each_allowlisted_pin_still_bypasses_the_variable(
    filename: str, job_id: str
) -> None:
    # Arrange
    job = _job(filename, job_id)
    # Act
    seam = reads_a_variable(job)
    # Assert
    assert not seam, f"{filename} -> {job_id} now reads a variable — drop its entry"
