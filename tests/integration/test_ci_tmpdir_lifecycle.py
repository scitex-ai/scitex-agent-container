"""The in-SIF CI scratch must be REMOVED — on this runner nothing else will.

MEASURED 2026-08-09 on scitex-04-cpu-01. ``.github/ci/run-in-sif.sh`` exported a
per-run ``TMPDIR`` under ``/tmp`` and nothing ever removed it. 116 survivors at
1.8-2.2 GB each put ``/tmp`` at 270 GB of a 393 GB root: root 100% full (39 MB
free), inodes at 92%, on a box hosting twelve fleet agents. ``build-in-sif.sh``
and ``publish-in-sif.sh`` leak identically, once per release. The defect is a
GitHub-hosted-runner assumption (the VM is discarded, so leaking is free)
applied to a persistent self-hosted one, where it is a slow outage.

These tests drive the REAL shell — ``tmpdir-lib.sh`` and ``clean-tmpdir.sh`` via
subprocess, no mocks — inside a sandbox root handed over by
``SAC_CI_TMPDIR_ROOT``, so nothing here can see, let alone touch, a real
``/tmp/ci-*`` directory.

The prune is the part of this fix that can CAUSE the outage it prevents: three
matrix legs run at once on one box and share ``GITHUB_RUN_ID``, so a sweep keyed
only on age deletes a sibling's LIVE scratch. Two tests carry MUTATION CONTROLS
— they strip a guard out of a COPY of the library and assert the sibling is then
destroyed. A guard that cannot be shown to fail proves nothing about the green.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "ci"
_LIB = _CI / "tmpdir-lib.sh"
_CLEAN = _CI / "clean-tmpdir.sh"
_EXEC = _CI / "exec-in-sif.sh"
_WORKFLOWS = _REPO / ".github" / "workflows"

# The run identity the sandbox pretends to be running under.
_RUN_ID = "77770001"
_ATTEMPT = "1"

# Comfortably past the 24 h floor. Used to backdate a directory's mtime.
_ANCIENT_S = 30 * 60 * 60

# The inner scripts that create a per-run scratch today.
_SCRATCH_CREATORS = ("run-in-sif.sh", "build-in-sif.sh", "publish-in-sif.sh")

# Paths ci_tmpdir_cleanup must refuse: the root itself, outside it, nested
# deeper, and traversal. Its argument comes from a workflow interpolation and
# goes to `rm -rf`.
_FORBIDDEN = (
    "<root>",
    "/",
    "<root>/site",
    "<root>/ci-scitex_agent_container-1-1-3.12/site",
    "<root>/../etc",
)


def _env(root: Path, **over: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "SAC_CI_TMPDIR_ROOT": str(root),
            "GITHUB_RUN_ID": _RUN_ID,
            "GITHUB_RUN_ATTEMPT": _ATTEMPT,
        }
    )
    env.update(over)
    return env


def _bash(script: str, root: Path, lib: Path | None = None):
    """Run a bash snippet with the lifecycle library sourced."""
    return subprocess.run(
        ["bash", "-c", f'set -uo pipefail; . "{lib or _LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env=_env(root),
    )


def _run_clean(root: Path, *args: str):
    return subprocess.run(
        ["bash", str(_CLEAN), *args], capture_output=True, text=True, env=_env(root)
    )


def _mkdir(root: Path, name: str, *, age_s: int = 0) -> Path:
    """Create a fake scratch dir with a payload, optionally backdated."""
    d = root / name
    (d / "site").mkdir(parents=True)
    (d / "site" / "payload.bin").write_bytes(b"x" * 1024)
    if age_s:
        when = time.time() - age_s
        os.utime(d, (when, when))
    return d


def _mutated_lib(tmp_path: Path, clause: str) -> Path:
    """A COPY of the library with one prune guard stripped out."""
    src = _LIB.read_text(encoding="utf-8")
    mutated = src.replace(clause, "")
    if mutated == src:
        raise AssertionError(f"guard clause not found to strip: {clause!r}")
    out = tmp_path / "mutated-tmpdir-lib.sh"
    out.write_text(mutated, encoding="utf-8")
    return out


def _leg(version: str) -> str:
    return f"ci-scitex_agent_container-{_RUN_ID}-{_ATTEMPT}-{version}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "scratch-root"
    r.mkdir()
    return r


# --- naming: ONE definition, shared by creator, remover and pruner ----------


@pytest.fixture(scope="module")
def path_result():
    return _bash("ci_tmpdir_path ci 3.12", Path("/sandbox-unused"))


def test_path_helper_exits_zero(path_result):
    # Arrange
    res = path_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stderr


def test_path_is_the_name_the_scripts_already_used(path_result):
    """Changing the name would start a SECOND leak beside the first and never
    reclaim the 116 directories already on disk."""
    # Arrange
    expected = f"/sandbox-unused/ci-scitex_agent_container-{_RUN_ID}-{_ATTEMPT}-3.12"
    # Act
    got = path_result.stdout
    # Assert
    assert got == expected


@pytest.mark.parametrize(
    ("inner", "prefix"),
    [
        ("run-in-sif.sh", "ci"),
        ("build-in-sif.sh", "build"),
        ("publish-in-sif.sh", "publish"),
        ("autobump-in-sif.sh", ""),
        ("not-a-script.sh", ""),
    ],
)
def test_prefix_table_maps_inner_script_to_prefix(root: Path, inner: str, prefix: str):
    # Arrange
    cmd = f'ci_tmpdir_prefix_for_inner "{inner}"'
    # Act
    res = _bash(cmd, root)
    # Assert
    assert res.stdout == prefix, res.stderr


# Function-scoped on purpose: STX-TQ004 rejects a module/session fixture that
# mutates in its body, and the accumulator here is exactly that shape. Re-globbing
# six shell scripts per test costs nothing.
@pytest.fixture
def scratch_creating_scripts() -> list[str]:
    """Scripts that assign TMPDIR — derived from the SCRIPTS, not the table."""
    found = []
    for script in sorted(_CI.glob("*-in-sif.sh")):
        if script.name == "exec-in-sif.sh":
            continue  # host-side wrapper; creates no scratch of its own
        src = script.read_text(encoding="utf-8")
        if re.search(r"^\s*(export\s+)?TMPDIR=", src, re.MULTILINE):
            found.append(script.name)
    return found


def test_scratch_creator_detection_finds_the_known_creators(scratch_creating_scripts):
    """Positive control: without it, an empty scan would pass every test
    below vacuously."""
    # Arrange
    expected = set(_SCRATCH_CREATORS)
    # Act
    found = set(scratch_creating_scripts)
    # Assert
    assert found >= expected, f"detection is broken; found {sorted(found)}"


def test_every_scratch_creating_script_is_in_the_prefix_table(
    root: Path, scratch_creating_scripts
):
    """The guard that survives the NEXT in-SIF script.

    Three scripts leaked for months because registering a new one for cleanup
    was left to whoever remembered. An unregistered creator now fails CI.
    """
    # Arrange
    missing = []
    # Act
    for name in scratch_creating_scripts:
        if not _bash(f'ci_tmpdir_prefix_for_inner "{name}"', root).stdout:
            missing.append(name)
    # Assert
    assert not missing, (
        f"{missing} create a per-run TMPDIR but are absent from "
        f"ci_tmpdir_prefix_for_inner in {_LIB} — they would leak unremoved"
    )


# --- cleanup: idempotent, concurrency-safe, refuses anything else ----------


@pytest.fixture
def cleanup_run(root: Path):
    """Create one scratch dir, remove it twice (the `always()` step and the
    next run's prune both target the same path)."""
    d = _mkdir(root, _leg("3.12"))
    first = _bash(f'ci_tmpdir_cleanup "{d}"', root)
    second = _bash(f'ci_tmpdir_cleanup "{d}"', root)
    return d, first, second


def test_cleanup_removes_the_directory(cleanup_run):
    # Arrange
    directory, _first, _second = cleanup_run
    # Act
    survived = directory.exists()
    # Assert
    assert not survived


def test_cleanup_exits_zero(cleanup_run):
    # Arrange
    _directory, first, _second = cleanup_run
    # Act
    rc = first.returncode
    # Assert
    assert rc == 0, first.stderr


def test_cleanup_is_idempotent_on_an_already_removed_path(cleanup_run):
    # Arrange
    _directory, _first, second = cleanup_run
    # Act
    rc = second.returncode
    # Assert
    assert rc == 0, second.stderr


def _forbidden_target(root: Path, spec: str) -> str:
    return spec.replace("<root>", str(root))


@pytest.mark.parametrize("spec", _FORBIDDEN)
def test_cleanup_refuses_a_path_it_did_not_create(root: Path, spec: str):
    # Arrange
    target = _forbidden_target(root, spec)
    # Act
    res = _bash(f'ci_tmpdir_cleanup "{target}"', root)
    # Assert
    assert res.returncode != 0, f"cleanup ACCEPTED {target!r}"


@pytest.mark.parametrize("spec", _FORBIDDEN)
def test_cleanup_refusal_says_why(root: Path, spec: str):
    # Arrange
    target = _forbidden_target(root, spec)
    # Act
    res = _bash(f'ci_tmpdir_cleanup "{target}"', root)
    # Assert
    assert "refusing to remove" in res.stderr


@pytest.mark.parametrize("spec", _FORBIDDEN)
def test_cleanup_refusal_destroys_nothing(root: Path, spec: str):
    # Arrange
    bystander = _mkdir(root, "ci-scitex_agent_container-1-1-3.12")
    # Act
    _bash(f'ci_tmpdir_cleanup "{_forbidden_target(root, spec)}"', root)
    # Assert
    assert bystander.is_dir() and root.is_dir()


# --- prune: the SIGKILL/reboot backstop, and the sibling-leg hazard --------


@pytest.fixture
def leftover_prune(root: Path):
    old = _mkdir(root, "ci-scitex_agent_container-11110000-1-3.12", age_s=_ANCIENT_S)
    return old, _bash("ci_tmpdir_prune", root)


def test_prune_removes_a_leftover_from_another_run(leftover_prune):
    # Arrange
    old, _res = leftover_prune
    # Act
    survived = old.exists()
    # Assert
    assert not survived


def test_prune_reports_what_it_removed(leftover_prune):
    """A silent destructive sweep on a shared node is how the NEXT incident
    gets misdiagnosed."""
    # Arrange
    old, res = leftover_prune
    # Act
    log = res.stdout
    # Assert
    assert "pruning leftover scratch" in log and old.name in log


def test_prune_exits_zero(leftover_prune):
    # Arrange
    _old, res = leftover_prune
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stderr


@pytest.mark.parametrize("version", ["3.11", "3.12", "3.13"])
def test_prune_spares_a_sibling_matrix_leg_of_this_run_even_when_old(
    root: Path, version: str
):
    """THE OUTAGE CASE. 3.11/3.12/3.13 start together and share GITHUB_RUN_ID.

    The release workflow's test job sets no ``timeout-minutes``, so a leg may
    legitimately live for GitHub's 6-hour default — backdated far past the age
    floor here on purpose. Only the run-identity guard can save it.
    """
    # Arrange
    leg = _mkdir(root, _leg(version), age_s=_ANCIENT_S)
    # Act
    _bash("ci_tmpdir_prune", root)
    # Assert
    assert leg.is_dir(), f"prune deleted a LIVE sibling matrix leg: {leg}"


def test_control_removing_the_run_identity_guard_destroys_the_sibling(
    root: Path, tmp_path: Path
):
    """MUTATION CONTROL for the test above — a guard that cannot be shown to
    fail proves nothing about the green."""
    # Arrange
    lib = _mutated_lib(tmp_path, '! -name "*-${run_id}-${attempt}-*" \\\n')
    leg = _mkdir(root, _leg("3.12"), age_s=_ANCIENT_S)
    # Act
    _bash("ci_tmpdir_prune", root, lib=lib)
    # Assert
    assert not leg.exists(), "the control cannot go red, so it validates nothing"


def test_prune_spares_a_young_directory_from_another_run(root: Path):
    """A different workflow run, concurrently in flight on the same runner."""
    # Arrange
    young = _mkdir(root, "ci-scitex_agent_container-99990000-1-3.12")
    # Act
    _bash("ci_tmpdir_prune", root)
    # Assert
    assert young.is_dir()


def test_control_dropping_the_age_floor_destroys_the_concurrent_run(
    root: Path, tmp_path: Path
):
    """MUTATION CONTROL for the age floor."""
    # Arrange
    lib = _mutated_lib(tmp_path, '-mmin "+${age_min}" \\\n')
    young = _mkdir(root, "ci-scitex_agent_container-99990000-1-3.12")
    # Act
    _bash("ci_tmpdir_prune", root, lib=lib)
    # Assert
    assert not young.exists(), "the control cannot go red, so it validates nothing"


@pytest.mark.parametrize(
    "name", ["apptainer-tmp-ywatanabe", "pytest-of-ywatanabe", "sac-tui-env-x.txt.d"]
)
def test_prune_leaves_unrelated_directories_alone(root: Path, name: str):
    """/tmp on that box holds 636 entries belonging to other things."""
    # Arrange
    foreign = _mkdir(root, name, age_s=_ANCIENT_S)
    # Act
    _bash("ci_tmpdir_prune", root)
    # Assert
    assert foreign.is_dir()


def test_prune_tolerates_a_missing_root(tmp_path: Path):
    """Hosted runners stay a live possibility (``vars.CI_RUNS_ON``): an absent
    or empty root must never fail a job."""
    # Arrange
    missing = tmp_path / "no-such-root"
    # Act
    res = _bash("ci_tmpdir_prune; echo rc=$?", missing)
    # Assert
    assert "rc=0" in res.stdout, res.stderr


@pytest.mark.parametrize("prefix", ["ci", "build", "publish"])
def test_prune_covers_every_leaking_prefix(root: Path, prefix: str):
    """One fix, all three leaking scripts — build and publish leak once per
    release, which is slower to notice, not less of a leak."""
    # Arrange
    old = _mkdir(
        root, f"{prefix}-scitex_agent_container-11110000-1-3.12", age_s=_ANCIENT_S
    )
    # Act
    _bash("ci_tmpdir_prune", root)
    # Assert
    assert not old.exists(), f"{old} survived the prune"


# --- clean-tmpdir.sh: the end-of-job step ---------------------------------


@pytest.fixture
def clean_run(root: Path):
    """The 3.12 leg cleans up while 3.11 and 3.13 are still running, and the
    same release run's build scratch exists alongside."""
    mine = _mkdir(root, _leg("3.12"))
    siblings = [_mkdir(root, _leg(v)) for v in ("3.11", "3.13")]
    build = _mkdir(root, f"build-scitex_agent_container-{_RUN_ID}-{_ATTEMPT}-3.12")
    res = _run_clean(root, "run-in-sif.sh", "3.12")
    return mine, siblings, build, res


def test_clean_removes_this_matrix_leg(clean_run):
    # Arrange
    mine, _siblings, _build, _res = clean_run
    # Act
    survived = mine.exists()
    # Assert
    assert not survived


def test_clean_exits_zero(clean_run):
    # Arrange
    _mine, _siblings, _build, res = clean_run
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stderr


def test_clean_spares_the_sibling_matrix_legs(clean_run):
    """All three legs share GITHUB_RUN_ID; a cleanup keyed on the run alone
    would delete the LIVE scratch of the two still running."""
    # Arrange
    _mine, siblings, _build, _res = clean_run
    # Act
    survivors = [s for s in siblings if s.is_dir()]
    # Assert
    assert survivors == siblings, "clean-tmpdir deleted a live sibling leg"


def test_clean_spares_the_build_scratch_of_the_same_run(clean_run):
    """`build` and `test` of one release run share the run id too."""
    # Arrange
    _mine, _siblings, build, _res = clean_run
    # Act
    survived = build.is_dir()
    # Assert
    assert survived


@pytest.fixture
def clean_missing(root: Path):
    return _run_clean(root, "run-in-sif.sh", "3.12")


def test_clean_reports_an_already_removed_directory(clean_missing):
    # Arrange
    res = clean_missing
    # Act
    log = res.stdout
    # Assert
    assert "already gone" in log


def test_clean_exits_zero_when_the_directory_is_already_gone(clean_missing):
    # Arrange
    res = clean_missing
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),  # no inner script
        ("autobump-in-sif.sh",),  # creates no scratch
        ("run-in-sif.sh",),  # no version -> cannot scope safely
    ],
)
def test_clean_never_fails_the_job(root: Path, args: tuple[str, ...]):
    """It is an `always()` step: it must never red a run, and must never
    replace the real failure of the job it is attached to."""
    # Arrange
    _mkdir(root, _leg("3.12"))
    # Act
    res = _run_clean(root, *args)
    # Assert
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    "args", [(), ("autobump-in-sif.sh",), ("run-in-sif.sh",), ("run-in-sif.sh", "")]
)
def test_clean_deletes_nothing_when_it_cannot_scope_the_target(
    root: Path, args: tuple[str, ...]
):
    """Without a version there is no way to tell this leg from its siblings, so
    the only safe move is to skip and let the next run's prune reclaim it."""
    # Arrange
    leg = _mkdir(root, _leg("3.12"))
    # Act
    _run_clean(root, *args)
    # Assert
    assert leg.is_dir(), "an unscoped invocation deleted a leg's scratch"


# --- a removal that FAILED must not be reported as a removal --------------
#
# The whole fix is verified in production by one log line — `clean-tmpdir:
# removing <path> (2.0G)`. An earlier draft printed that line whether or not
# anything was reclaimed, because ci_tmpdir_cleanup swallowed rm's status. On a
# sticky /tmp shared by twelve agents, "I could not remove it" is exactly the
# case that must be visible.

_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the permission bits these tests rely on"
)


@pytest.fixture
def denied_subtree(root: Path):
    """The realistic failure: ~10 tests in this suite chmod a ``tmp_path``
    directory to 0o555 and restore it in a ``finally``. A worker SIGKILLed in
    between — the cancellation path this fix exists for — leaves that behind."""
    scratch = _mkdir(root, _leg("3.12"))
    denied = scratch / "pytest-of-ci" / "pytest-0" / "test_x0" / "denied"
    denied.mkdir(parents=True)
    (denied / "payload").write_text("x", encoding="utf-8")
    denied.chmod(0o555)
    try:
        yield scratch, _bash(f'ci_tmpdir_cleanup "{scratch}"; echo rc=$?', root)
    finally:
        if denied.exists():
            denied.chmod(0o755)


@_AS_ROOT
def test_cleanup_reclaims_a_subtree_left_non_writable(denied_subtree):
    # Arrange
    scratch, _res = denied_subtree
    # Act
    survived = scratch.exists()
    # Assert
    assert not survived, "an unwritable subtree defeated the cleanup"


@_AS_ROOT
def test_cleanup_exits_zero_after_reclaiming_a_non_writable_subtree(denied_subtree):
    # Arrange
    _scratch, res = denied_subtree
    # Act
    log = res.stdout
    # Assert
    assert "rc=0" in log, res.stderr


@pytest.fixture
def unremovable(root: Path):
    """A scratch dir inside a READ-ONLY root: no chmod of ours can unlink it,
    so this is a removal that genuinely cannot succeed."""
    scratch = _mkdir(root, _leg("3.12"))
    root.chmod(0o500)
    try:
        yield scratch
    finally:
        root.chmod(0o700)


@_AS_ROOT
def test_cleanup_reports_a_removal_it_could_not_perform(root: Path, unremovable: Path):
    # Arrange
    target = unremovable
    # Act
    res = _bash(f'ci_tmpdir_cleanup "{target}"; echo rc=$?', root)
    # Assert
    assert "rc=1" in res.stdout, f"a failed removal returned success: {res.stdout!r}"


@_AS_ROOT
def test_clean_step_warns_when_the_removal_failed(root: Path, unremovable: Path):
    """clean-tmpdir.sh's `::warning::` branch was unreachable while cleanup
    always returned 0 — the step logged a removal that never happened."""
    # Arrange
    _target = unremovable
    # Act
    res = _run_clean(root, "run-in-sif.sh", "3.12")
    # Assert
    assert "::warning::" in res.stdout + res.stderr, res.stdout


@_AS_ROOT
def test_clean_step_still_exits_zero_when_the_removal_failed(
    root: Path, unremovable: Path
):
    """Honest, but still never reds the job it is `always()`-attached to."""
    # Arrange
    _target = unremovable
    # Act
    res = _run_clean(root, "run-in-sif.sh", "3.12")
    # Assert
    assert res.returncode == 0, res.stderr


def test_clean_refuses_an_unmanaged_target_before_announcing_it(root: Path):
    """A malformed version composed a path outside the managed set, and the
    step announced `removing <root> (270G)` before refusing it — a line that
    reads like an imminent disaster and burns the 30 s `du` bound saying so."""
    # Arrange
    _bystander = _mkdir(root, _leg("3.12"))
    # Act
    res = _run_clean(root, "run-in-sif.sh", "../..")
    # Assert
    assert "removing" not in res.stdout, res.stdout


def test_clean_says_so_when_the_library_cannot_be_loaded(tmp_path: Path):
    """A partial checkout used to produce "creates no per-run scratch" — the
    opposite of the truth, while a directory leaked."""
    # Arrange
    orphan = tmp_path / "ci-no-lib"
    orphan.mkdir()
    (orphan / "clean-tmpdir.sh").write_text(
        _CLEAN.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # Act
    res = subprocess.run(
        ["bash", str(orphan / "clean-tmpdir.sh"), "run-in-sif.sh", "3.12"],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
    )
    # Assert
    assert "could not load" in res.stdout + res.stderr, res.stdout


# --- the age knob is clamped: its failure mode is destructive --------------


def _prune_with_age(root: Path, age: str):
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; . "{_LIB}"\nci_tmpdir_prune; echo rc=$?'],
        capture_output=True,
        text=True,
        env=_env(root, SAC_CI_TMPDIR_MAX_AGE_H=age),
    )


@pytest.mark.parametrize("age", ["0", "abc", "-1", "", "2.5"])
def test_prune_ignores_an_age_override_that_is_not_a_floor(root: Path, age: str):
    """``=0`` deleted a 3-minute-old concurrent run's LIVE scratch — the exact
    outage this fix exists to prevent, reachable from one stray env var."""
    # Arrange
    live = _mkdir(root, "ci-scitex_agent_container-99990000-1-3.12")
    # Act
    _prune_with_age(root, age)
    # Assert
    assert live.is_dir(), f"SAC_CI_TMPDIR_MAX_AGE_H={age!r} destroyed a live run"


@pytest.mark.parametrize("age", ["abc", "", "2.5"])
def test_prune_survives_a_malformed_age_override_under_errexit(root: Path, age: str):
    """``=abc`` aborted with `abc: unbound variable`, and exec-in-sif.sh runs
    under `set -euo pipefail` — so it failed the job before the SIF started."""
    # Arrange
    _mkdir(root, "ci-scitex_agent_container-99990000-1-3.12")
    # Act
    res = _prune_with_age(root, age)
    # Assert
    assert "rc=0" in res.stdout, res.stderr


def test_prune_still_reclaims_a_leftover_under_a_valid_age_override(root: Path):
    """The clamp must not neuter the knob the tests above rely on."""
    # Arrange
    old = _mkdir(root, "ci-scitex_agent_container-11110000-1-3.12", age_s=2 * 3600)
    # Act
    _prune_with_age(root, "1")
    # Assert
    assert not old.exists(), "the clamp swallowed a legitimate age override"


# --- wiring: every job that creates scratch must also remove it ------------


def _steps_of(workflow: Path):
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for job_name, job in (doc.get("jobs") or {}).items():
        yield job_name, (job.get("steps") or [])


def _tail_invocation(run: str, script: str):
    m = re.search(rf"{re.escape(script)}\s+(\S+)((?:\s+\S+)*)\s*$", run.strip())
    return (m.group(1), m.group(2).strip()) if m else None


# Function-scoped for the same reason as `scratch_creating_scripts` above.
@pytest.fixture
def wiring():
    """Every (workflow, job, inner, args) that creates scratch, paired with the
    cleanup invocations present in the same job."""
    pairs = []
    for wf in sorted(_WORKFLOWS.glob("*.y*ml")):
        for job_name, steps in _steps_of(wf):
            cleans = []
            for step in steps:
                hit = _tail_invocation(step.get("run") or "", "clean-tmpdir.sh")
                if hit:
                    cleans.append((*hit, str(step.get("if", ""))))
            for step in steps:
                hit = _tail_invocation(step.get("run") or "", "exec-in-sif.sh")
                if hit and hit[0] in _SCRATCH_CREATORS:
                    pairs.append((f"{wf.name}:{job_name}", hit, cleans))
    return pairs


def test_workflow_scan_finds_the_scratch_creating_steps(wiring):
    """Positive control: an empty scan would pass the two tests below
    vacuously — exactly the failure mode this whole fix is about."""
    # Arrange
    expected_minimum = 4  # 2x pytest-matrix/release test legs, build, publish
    # Act
    found = len(wiring)
    # Assert
    assert found >= expected_minimum, f"scanned only {found} scratch-creating steps"


def test_every_scratch_creating_job_has_a_cleanup_step(wiring):
    """The discipline that failed here, converted into a CI failure."""
    # Arrange
    unpaired = []
    # Act
    for where, (inner, args), cleans in wiring:
        if not [c for c in cleans if c[0] == inner and c[1] == args]:
            unpaired.append(f"{where} runs {inner} {args}")
    # Assert
    assert not unpaired, (
        f"{unpaired} — no matching `clean-tmpdir.sh` step, so each leaks "
        f"~2 GB of scratch per run on the persistent runner"
    )


def test_every_cleanup_step_is_guarded_by_always(wiring):
    """Without `always()` the step skips on FAILURE and on CANCELLATION — the
    two endings that most need the disk back."""
    # Arrange
    unguarded = []
    # Act
    for where, (inner, args), cleans in wiring:
        match = [c for c in cleans if c[0] == inner and c[1] == args]
        if match and not any("always()" in c[2] for c in match):
            unguarded.append(f"{where} ({inner} {args})")
    # Assert
    assert not unguarded, f"cleanup steps not guarded by `if: always()`: {unguarded}"


def test_exec_wrapper_sources_the_lifecycle_library():
    # Arrange
    src = _EXEC.read_text(encoding="utf-8")
    # Act
    sourced = "tmpdir-lib.sh" in src
    # Assert
    assert sourced, "exec-in-sif.sh no longer sources the lifecycle library"


def test_exec_wrapper_runs_the_startup_prune():
    """The ONLY cover for SIGKILL and reboot, where no in-process cleanup can
    run by construction (the runner unit is KillMode=process)."""
    # Arrange
    src = _EXEC.read_text(encoding="utf-8")
    # Act
    calls = re.search(r"^ci_tmpdir_prune\s*$", src, re.MULTILINE)
    # Assert
    assert calls, (
        "exec-in-sif.sh no longer calls ci_tmpdir_prune — leftovers from a "
        "SIGKILLed or rebooted run would accumulate forever"
    )


def test_no_inner_script_hardcodes_its_own_scratch_path():
    """Two spellings of the name is how a cleanup starts missing its target."""
    # Arrange
    offenders = []
    # Act
    for script in sorted(_CI.glob("*-in-sif.sh")):
        for line in script.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#") and re.search(r'TMPDIR="?/tmp/', line):
                offenders.append(f"{script.name}: {line.strip()}")
    # Assert
    assert not offenders, (
        "these build a scratch path literally instead of via ci_tmpdir_path, so "
        f"clean-tmpdir.sh and the prune cannot find it: {offenders}"
    )
