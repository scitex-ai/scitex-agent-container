"""``exec-in-sif.sh`` must adapt to the host, and the proof must break the branch.

THE DEFECT. The shim hardcoded Spartan's shared filesystem in two places that
run before anything else can::

    export APPTAINER_TMPDIR="/data/gpfs/projects/punim0264/.../apptainer-tmp"
    mkdir -p "$APPTAINER_TMPDIR"
    exec "$APPTAINER" exec --pwd "$PWD" --bind /data/gpfs/projects/punim0264 ...

``/data/gpfs/projects/punim0264`` does not exist on the local compute nodes.
Under ``set -euo pipefail`` the ``mkdir`` aborts the job before a single test
runs, and past it apptainer REFUSES a ``--bind`` whose source is absent. NOT the
apptainer resolution, which was already defused: the variable is set,
``/usr/bin/apptainer`` exists, and the useless ``~/.env-3.11`` PATH prepend is
harmless rather than fatal. The scratch dir and the bind are the killers.

WHY THESE TESTS ARE SHAPED LIKE THIS. The obvious check is VACUOUS: running the
shim where the GPFS path happens to exist proves nothing about the host where it
does not, and vice versa. So each branch is proven by MAKING ITS CONDITION
FALSE — the opposite-host shape is constructed in a mount namespace with a
tmpfs over ``/data``, which both CREATES the GPFS tree on a machine that lacks
it and HIDES it on Spartan, which has it. The namespace dies with the process,
so neither shape can leak onto the real host.

MUTATION CONTROLS. ``test_mutant_*`` strip the host branch back out of a COPY of
the shim — restoring the exact pre-fix lines — and assert the copy then dies the
way the real one did. A test that passes both before and after the change is
worth nothing, so the guards are shown failing, not merely passing.

NO MOCKS FOR THE BEHAVIOUR ASSERTED. The real ``exec-in-sif.sh`` runs, in a real
shell, and makes every decision under test itself: which apptainer, which
scratch directory, whether ``--bind`` appears. The only substitution is at the
FINAL exec boundary, where ``SCITEX_CI_APPTAINER`` names a recorder that writes
its argv and exits — the observation point for the argv the shim really built,
not a stand-in for any decision the shim makes.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "ci"
_EXEC = _CI / "exec-in-sif.sh"
_LIB = _CI / "tmpdir-lib.sh"

_GPFS = "/data/gpfs/projects/punim0264"

# The shim shells out to these. The no-apptainer-anywhere case needs a PATH that
# provably holds no apptainer, so it gets a sandbox bin with exactly this set.
_NEEDED_TOOLS = (
    "bash",
    "cat",
    "chmod",
    "dirname",
    "find",
    "grep",
    "mkdir",
    "pkill",
    "rm",
    "sed",
    "sort",
    "stat",
)

# Resolved from the REAL PATH at import. The sandbox deliberately hands the shim
# a PATH holding only _NEEDED_TOOLS (so "no apptainer anywhere" is a fact, not a
# hope), and the namespace helpers are launched with that same env — so they
# must be named absolutely or they are simply not found.
_UNSHARE = shutil.which("unshare")
_MOUNT = shutil.which("mount")
_MKDIR = shutil.which("mkdir")
_BASH = shutil.which("bash") or "/bin/bash"

_RECORDER = """#!/usr/bin/env bash
# Observation point at the exec boundary: record the argv the shim built.
printf '%s\\n' "$@" > "{argv_file}"
exit 0
"""

# Restores the pre-fix scratch line: unconditional GPFS APPTAINER_TMPDIR.
_MUTATE_SCRATCH = (
    'if [ -d "$GPFS_PROJECT" ]; then\n'
    '    export APPTAINER_TMPDIR="$GPFS_PROJECT/ywatanabe/ci/apptainer-tmp"\n'
    "else\n"
    '    export APPTAINER_TMPDIR="$HOME/.cache/scitex-ci/apptainer-tmp"\n'
    "fi",
    'export APPTAINER_TMPDIR="$GPFS_PROJECT/ywatanabe/ci/apptainer-tmp"',
)

# Restores the pre-fix bind: unconditional --bind of the GPFS tree.
_MUTATE_BIND = (
    'if [ -d "$GPFS_PROJECT" ]; then\n    APPTAINER_ARGV+=(--bind "$GPFS_PROJECT")',
    'if true; then\n    APPTAINER_ARGV+=(--bind "$GPFS_PROJECT")',
)


def _mutate(text: str, mutation: tuple[str, str]) -> str:
    old, new = mutation
    assert old in text, f"mutation anchor no longer present in the shim:\n{old}"
    return text.replace(old, new, 1)


class _Sandbox:
    """A fake checkout + HOME + PATH the real shim is driven inside."""

    def __init__(self, root: Path, shim_text: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.checkout = root / "checkout"
        self.ci = self.checkout / ".github" / "ci"
        self.ci.mkdir(parents=True)
        self.home = root / "home"
        self.home.mkdir()
        self.tmproot = root / "tmproot"
        self.tmproot.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.argv_file = root / "argv.txt"

        self.shim = self.ci / "exec-in-sif.sh"
        self.shim.write_text(shim_text)
        self.shim.chmod(0o755)
        # Only ``develop`` carries tmpdir-lib.sh, and only its shim sources it.
        # ``main`` has neither, so this file must run unchanged on both — the
        # promotion into main is the branch that matters, and a test that only
        # collects on develop cannot gate it.
        if _LIB.exists():
            shutil.copy2(_LIB, self.ci / "tmpdir-lib.sh")
        (self.ci / "inner.sh").write_text("#!/usr/bin/env bash\necho inner ran\n")

        # The shim only checks `[ -f "$SIF" ]`; it never opens the image.
        self.sif = root / "ci-cpu.sif"
        self.sif.write_text("not a real SIF, and the shim never reads one")

        self.recorder = root / "recorder"
        self.recorder.write_text(_RECORDER.format(argv_file=self.argv_file))
        self.recorder.chmod(0o755)

        for tool in _NEEDED_TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)

    def env(self, *, apptainer: str | None, path_apptainer: bool) -> dict[str, str]:
        if path_apptainer:
            link = self.bin / "apptainer"
            if not link.exists():
                link.symlink_to(self.recorder)
        env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "SCITEX_CI_SIF": str(self.sif),
            "SAC_CI_TMPDIR_ROOT": str(self.tmproot),
            "LC_ALL": "C",
        }
        if apptainer is not None:
            env["SCITEX_CI_APPTAINER"] = apptainer
        return env

    def argv(self) -> list[str]:
        if not self.argv_file.exists():
            return []
        return self.argv_file.read_text().splitlines()


@dataclass
class _Outcome:
    sandbox: _Sandbox
    result: subprocess.CompletedProcess

    @property
    def output(self) -> str:
        return self.result.stdout + self.result.stderr


def _reshape_script(gpfs: bool) -> str:
    """Replace ``/data`` with a tmpfs, then create the GPFS tree or not.

    This is what makes the branch provable: on a compute node it CONSTRUCTS the
    Spartan shape, and on Spartan it HIDES the real GPFS tree. Both directions
    vanish with the mount namespace when the process exits.

    THE ABSENT SHAPE'S TMPFS IS MOUNTED READ-ONLY, and that detail is
    load-bearing. A writable tmpfs is NOT a compute node: inside a user
    namespace we are mapped-root, so the shim's own ``mkdir -p
    /data/gpfs/.../apptainer-tmp`` SUCCEEDS on it — creating the very GPFS tree
    whose absence is the whole scenario, after which the later ``[ -d ]`` bind
    probe sees it and takes the Spartan branch. Measured here: the scratch
    mutation SURVIVED exactly that way, and the mutation control is what
    exposed it. On a real compute node ``/`` is root-owned and the mkdir fails
    with EACCES; ``-o ro`` is how the tmpfs reproduces that refusal, and
    MS_RDONLY is enforced against mapped-root where a file mode would not be.
    """
    prelude = f"{_MKDIR} -p /data 2>/dev/null || true; "
    if gpfs:
        return (
            prelude
            + f"{_MOUNT} -t tmpfs tmpfs /data && {_MKDIR} -p {_GPFS} && exec \"$@\""
        )
    return prelude + f"{_MOUNT} -t tmpfs -o ro tmpfs /data && exec \"$@\""


def _run(sb: _Sandbox, env: dict[str, str], *, gpfs: bool | None = None) -> _Outcome:
    """Run the shim. ``gpfs`` None = this host as-is; True/False = constructed."""
    argv = [_BASH, str(sb.shim), "inner.sh", "3.12"]
    if gpfs is not None:
        argv = [
            _UNSHARE,
            "--map-root-user",
            "--mount",
            _BASH,
            "-c",
            _reshape_script(gpfs),
            "--",
            *argv,
        ]
    return _Outcome(
        sb,
        subprocess.run(
            argv, cwd=sb.checkout, env=env, capture_output=True, text=True, timeout=120
        ),
    )


def _namespace_usable() -> bool:
    """Can the opposite host shape be constructed here? Report, never pretend."""
    if not (_UNSHARE and _MOUNT and _MKDIR):
        return False
    # Both shapes must be constructible, INCLUDING the read-only remount — a
    # namespace that only gives us a writable tmpfs cannot represent a compute
    # node, and would let the mutation controls pass for the wrong reason.
    checks = (
        _reshape_script(gpfs=True).replace(' && exec "$@"', ""),
        _reshape_script(gpfs=False).replace(' && exec "$@"', "")
        + f" && ! {_MKDIR} -p {_GPFS}",
    )
    for script in checks:
        try:
            probe = subprocess.run(
                [_UNSHARE, "--map-root-user", "--mount", _BASH, "-c", script],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if probe.returncode != 0:
            return False
    return True


_NS_REASON = (
    "no usable mount namespace here, so the opposite host shape cannot be "
    "constructed. The branch is then UNPROVEN on this machine rather than "
    "passing vacuously."
)


def _require_namespace() -> None:
    if not _namespace_usable():
        pytest.skip(_NS_REASON)


@pytest.fixture(scope="module")
def shim_text() -> str:
    return _EXEC.read_text()


def _case(root, shim, *, gpfs=None, apptainer="recorder", on_path=False, sif=None):
    sb = _Sandbox(root, shim)
    resolved = str(sb.recorder) if apptainer == "recorder" else apptainer
    env = sb.env(apptainer=resolved, path_apptainer=on_path)
    if sif is not None:
        env["SCITEX_CI_SIF"] = sif
    return _run(sb, env, gpfs=gpfs)


@pytest.fixture(scope="module")
def compute(tmp_path_factory, shim_text) -> _Outcome:
    _require_namespace()
    return _case(tmp_path_factory.mktemp("compute"), shim_text, gpfs=False)


@pytest.fixture(scope="module")
def spartan(tmp_path_factory, shim_text) -> _Outcome:
    _require_namespace()
    return _case(tmp_path_factory.mktemp("spartan"), shim_text, gpfs=True)


@pytest.fixture(scope="module")
def mutant_scratch_on_compute(tmp_path_factory, shim_text) -> _Outcome:
    _require_namespace()
    text = _mutate(shim_text, _MUTATE_SCRATCH)
    return _case(tmp_path_factory.mktemp("mut_scratch"), text, gpfs=False)


@pytest.fixture(scope="module")
def mutant_bind_on_compute(tmp_path_factory, shim_text) -> _Outcome:
    _require_namespace()
    text = _mutate(shim_text, _MUTATE_BIND)
    return _case(tmp_path_factory.mktemp("mut_bind"), text, gpfs=False)


@pytest.fixture(scope="module")
def configured_apptainer(tmp_path_factory, shim_text) -> _Outcome:
    return _case(tmp_path_factory.mktemp("cfg_appt"), shim_text)


@pytest.fixture(scope="module")
def foreign_apptainer_path(tmp_path_factory, shim_text) -> _Outcome:
    """SCITEX_CI_APPTAINER names Spartan's shim on a node that has no such file."""
    return _case(
        tmp_path_factory.mktemp("foreign_appt"),
        shim_text,
        apptainer="~/.env-3.11/bin/apptainer",
        on_path=True,
    )


@pytest.fixture(scope="module")
def no_apptainer(tmp_path_factory, shim_text) -> _Outcome:
    root = tmp_path_factory.mktemp("no_appt")
    return _case(root, shim_text, apptainer=str(root / "nowhere" / "apptainer"))


@pytest.fixture(scope="module")
def missing_sif(tmp_path_factory, shim_text) -> _Outcome:
    root = tmp_path_factory.mktemp("no_sif")
    return _case(root, shim_text, sif=str(root / "absent-ci-cpu.sif"))


# --------------------------------------------------------------------------
# Compute-node shape: GPFS made absent.
# --------------------------------------------------------------------------


def test_compute_shape_runs_to_the_exec(compute):
    # Arrange
    outcome = compute
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc == 0, f"the shim died on a compute-shaped host:\n{outcome.output}"


def test_compute_shape_reports_the_gpfs_absent_profile(compute):
    # Arrange
    expected = f"exec-in-sif: {_GPFS} absent (scratch under $HOME, no GPFS bind)"
    # Act
    stdout = compute.result.stdout
    # Assert
    assert expected in stdout, f"the taken profile was not reported:\n{stdout}"


def test_compute_shape_omits_the_gpfs_bind(compute):
    # Arrange
    outcome = compute
    # Act
    argv = outcome.sandbox.argv()
    # Assert
    assert "--bind" not in argv, (
        "a --bind survived where GPFS is absent; apptainer refuses a bind whose "
        f"source does not exist. argv={argv}"
    )


def test_compute_shape_puts_scratch_under_home(compute):
    # Arrange
    expected = compute.sandbox.home / ".cache" / "scitex-ci" / "apptainer-tmp"
    # Act
    stdout = compute.result.stdout
    # Assert
    assert f"exec-in-sif: APPTAINER_TMPDIR={expected}" in stdout


def test_compute_shape_actually_creates_that_scratch(compute):
    # Arrange
    expected = compute.sandbox.home / ".cache" / "scitex-ci" / "apptainer-tmp"
    # Act
    created = expected.is_dir()
    # Assert
    assert created, "the shim reported a scratch directory it never created"


def test_compute_shape_still_execs_the_sif(compute):
    # Arrange
    outcome = compute
    # Act
    argv = outcome.sandbox.argv()
    # Assert
    assert str(outcome.sandbox.sif) in argv, f"the SIF left the argv: {argv}"


# --------------------------------------------------------------------------
# Spartan shape: GPFS made present. Unchanged behaviour is the requirement.
# --------------------------------------------------------------------------


def test_spartan_shape_runs_to_the_exec(spartan):
    # Arrange
    outcome = spartan
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc == 0, f"the shim died on a Spartan-shaped host:\n{outcome.output}"


def test_spartan_shape_reports_the_gpfs_present_profile(spartan):
    # Arrange
    expected = f"exec-in-sif: {_GPFS} present (scratch on GPFS, punim0264 bound)"
    # Act
    stdout = spartan.result.stdout
    # Assert
    assert expected in stdout, f"the taken profile was not reported:\n{stdout}"


def test_spartan_shape_keeps_scratch_on_gpfs(spartan):
    # Arrange
    expected = f"exec-in-sif: APPTAINER_TMPDIR={_GPFS}/ywatanabe/ci/apptainer-tmp"
    # Act
    stdout = spartan.result.stdout
    # Assert
    assert expected in stdout


def test_spartan_shape_binds_the_gpfs_project(spartan):
    # Arrange
    argv = spartan.sandbox.argv()
    # Act
    bound = argv[argv.index("--bind") + 1] if "--bind" in argv else None
    # Assert
    assert bound == _GPFS, f"the GPFS bind was dropped where GPFS exists: {argv}"


def test_the_two_host_shapes_produce_different_argv(compute, spartan):
    """Guards the harness: identical argv would mean nothing above is measured."""
    # Arrange
    on_compute = compute.sandbox.argv()
    # Act
    on_spartan = spartan.sandbox.argv()
    # Assert
    assert on_compute != on_spartan, (
        "both host shapes produced an identical argv — the namespace is not "
        "changing what the shim sees, so these tests prove nothing"
    )


# --------------------------------------------------------------------------
# Mutation controls: put the defect back and watch it kill the job.
# --------------------------------------------------------------------------


def test_mutant_unconditional_gpfs_scratch_aborts_on_compute(mutant_scratch_on_compute):
    """The pre-fix scratch line restored: `mkdir -p` on GPFS under `set -e`."""
    # Arrange
    outcome = mutant_scratch_on_compute
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc != 0, (
        "the unconditional GPFS scratch SURVIVED where GPFS is absent — then the "
        "conditional in the real shim is not what makes it work, and every "
        f"passing test above is measuring the wrong thing:\n{outcome.output}"
    )


def test_mutant_unconditional_gpfs_scratch_never_reaches_the_exec(
    mutant_scratch_on_compute,
):
    # Arrange
    outcome = mutant_scratch_on_compute
    # Act
    argv = outcome.sandbox.argv()
    # Assert
    assert not argv, f"the mutant reached the exec; it should die at mkdir: {argv}"


def test_mutant_unconditional_bind_is_passed_where_gpfs_is_absent(
    mutant_bind_on_compute,
):
    """The pre-fix bind restored: apptainer would refuse this argv outright."""
    # Arrange
    argv = mutant_bind_on_compute.sandbox.argv()
    # Act
    bound = argv[argv.index("--bind") + 1] if "--bind" in argv else None
    # Assert
    assert bound == _GPFS, (
        "the bind mutation did not take effect, so the conditional bind passing "
        f"above is not evidence that it is what removes the failure: {argv}"
    )


# --------------------------------------------------------------------------
# Apptainer resolution: honour the variable, fall back to PATH, fail loudly.
# --------------------------------------------------------------------------


def test_configured_apptainer_is_used_when_executable(configured_apptainer):
    # Arrange
    sb = configured_apptainer.sandbox
    # Act
    stdout = configured_apptainer.result.stdout
    # Assert
    assert f"exec-in-sif: apptainer={sb.recorder} (via SCITEX_CI_APPTAINER)" in stdout


def test_foreign_configured_path_is_not_fatal(foreign_apptainer_path):
    """Spartan's ~/.env-3.11 shim named on a compute node must not kill the job."""
    # Arrange
    outcome = foreign_apptainer_path
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc == 0, (
        "a configured apptainer path belonging to another host was treated as "
        f"fatal even though apptainer is on PATH:\n{outcome.output}"
    )


def test_foreign_configured_path_falls_back_to_path(foreign_apptainer_path):
    # Arrange
    outcome = foreign_apptainer_path
    # Act
    stdout = outcome.result.stdout
    # Assert
    assert "(via PATH)" in stdout, f"the PATH fallback was not taken:\n{stdout}"


def test_no_apptainer_anywhere_is_a_hard_error(no_apptainer):
    # Arrange
    outcome = no_apptainer
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc != 0, f"a runner with no apptainer was allowed through:\n{outcome.output}"


def test_no_apptainer_error_names_the_configured_attempt(no_apptainer):
    # Arrange
    outcome = no_apptainer
    # Act
    out = outcome.output
    # Assert
    assert "SCITEX_CI_APPTAINER" in out, f"first attempt not named:\n{out}"


def test_no_apptainer_error_names_the_path_attempt(no_apptainer):
    # Arrange
    outcome = no_apptainer
    # Act
    out = outcome.output
    # Assert
    assert "on PATH" in out, f"second attempt not named:\n{out}"


def test_no_apptainer_error_says_what_would_fix_it(no_apptainer):
    # Arrange
    outcome = no_apptainer
    # Act
    out = outcome.output
    # Assert
    assert "Install apptainer on this runner" in out, f"no remedy offered:\n{out}"


def test_no_apptainer_never_reaches_the_exec(no_apptainer):
    # Arrange
    outcome = no_apptainer
    # Act
    argv = outcome.sandbox.argv()
    # Assert
    assert not argv, f"the shim exec'd something despite no apptainer: {argv}"


def test_missing_sif_is_a_hard_error(missing_sif):
    # Arrange
    outcome = missing_sif
    # Act
    rc = outcome.result.returncode
    # Assert
    assert rc != 0, f"a missing SIF was allowed through:\n{outcome.output}"


def test_missing_sif_error_names_the_path_it_looked_at(missing_sif):
    # Arrange
    outcome = missing_sif
    # Act
    out = outcome.output
    # Assert
    assert "absent-ci-cpu.sif" in out, f"the missing SIF was not named:\n{out}"


# --------------------------------------------------------------------------
# The shipped file itself, on whatever branch this checkout is.
# --------------------------------------------------------------------------


def test_shipped_shim_uses_gpfs_only_behind_the_probe():
    """The regression in one line, and what a promotion to ``main`` must satisfy.

    ``main`` carried the Spartan-only copy long after ``develop`` was fixed, and
    a green ``develop`` says nothing about a tag — tags are cut from ``main``.
    """
    # Arrange
    lines = [
        ln.strip()
        for ln in _EXEC.read_text().splitlines()
        if _GPFS in ln and not ln.strip().startswith("#")
    ]
    # Act
    unguarded = [ln for ln in lines if not ln.startswith('GPFS_PROJECT="')]
    # Assert
    assert not unguarded, (
        "GPFS path used outside the single probed assignment, so the host branch "
        f"can be bypassed: {unguarded}"
    )


def test_shipped_shim_probes_the_gpfs_directory():
    # Arrange
    text = _EXEC.read_text()
    # Act
    probed = 'if [ -d "$GPFS_PROJECT" ]' in text
    # Assert
    assert probed, "the host is declared rather than detected"


def test_shipped_shim_is_syntactically_valid():
    # Arrange
    cmd = ["bash", "-n", str(_EXEC)]
    # Act
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    # Assert
    assert res.returncode == 0, res.stderr
