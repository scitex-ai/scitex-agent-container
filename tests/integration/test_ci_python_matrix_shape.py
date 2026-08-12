"""The three python matrices must stay in agreement, or a bump half-applies.

THE SHAPE (operator, 2026-08-12)::

    PR gate    3.11 + 3.13          our pool      a human waits  -> LATENCY
    nightly    3.12                 GitHub-hosted nobody waits   -> COST
    release    3.11 + 3.12 + 3.13   our pool      correctness    -> COVERAGE

WHY THE ENDS AND NOT ONE VERSION. Running a single leg was proposed and
rejected the same night, on our own evidence: scitex-dev #578 (a fix for
``LD_LIBRARY_PATH`` being stripped from child processes, which took out 21
tests) failed on 3.11 and 3.13 and PASSED on 3.12 -- only the ends link
libpython dynamically. A gate running the middle alone would have shipped it.

WHY THIS FILE EXISTS. The three lists live in three workflow files, and GitHub
Actions gives no way to define them once: ``strategy`` cannot read the ``env``
context, so the only single-source spellings are a repository Variable (invisible
to the repo, and a settings change can then silently re-point CI) or an extra
setup job (a whole runner slot of queue latency on a saturated pool, to save
three literals). Both are worse than the duplication. So the lists are DERIVED
FROM EACH OTHER HERE instead:

* the PR gate must be exactly ``[oldest, newest]`` of the full set,
* the nightly must be exactly the full set MINUS the PR gate,
* the release must be exactly the full set, with no event condition,
* the full set must agree with what ``pyproject.toml`` declares.

Bump python and touch only one file and this goes RED. That is the whole point:
a half-applied version bump is invisible in review and stays invisible until a
release, and this fleet keeps hitting exactly that shape.

KNOWN, DELIBERATE GAP -- NOT A BUG THIS FILE HIDES: ``requires-python`` declares
``>=3.10`` and the classifiers list 3.10, but CI's floor is 3.11 because the
shared ``ci-cpu.sif`` bakes only 3.11/3.12/3.13 at ``/opt/venv-<ver>``. So 3.10
is DECLARED supported and never tested. ``test_ci_never_tests_an_undeclared_
version`` asserts the safe direction (CI never tests something we do not claim
to support); closing the other direction means either baking 3.10 into the SIF
or raising ``requires-python``, and that is a separate decision with its own
blast radius.

No mocks: every assertion parses the real ``.github/workflows/*.y*ml`` and the
real ``pyproject.toml``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

PR_GATE_WORKFLOW = WORKFLOW_DIR / "pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"
NIGHTLY_WORKFLOW = WORKFLOW_DIR / "nightly-python-matrix-on-github-hosted.yml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "pypi-publish-and-github-release-on-tag.yml"

# Every quoted JSON array inside a ${{ }} expression, in source order.
_JSON_ARRAY_RE = re.compile(r"'(\[[^']*\])'")
_CLASSIFIER_RE = re.compile(r"^Programming Language :: Python :: (\d+\.\d+)$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _matrix_raw(path: Path, job: str = "test") -> object:
    return _load(path)["jobs"][job]["strategy"]["matrix"]["python-version"]


def _key(version: str) -> tuple[int, ...]:
    """Sort 3.9 BELOW 3.11 -- string order would put it above."""
    return tuple(int(part) for part in version.split("."))


def _sorted(versions: object) -> list[str]:
    return sorted((str(v) for v in versions), key=_key)


def _conditional_matrix(path: Path) -> tuple[list[str], list[str]]:
    """Split the PR gate's conditional expression into (pr_list, full_list).

    The shipped spelling is one ``fromJSON`` over two string literals::

        ${{ fromJSON(github.event_name == 'pull_request'
                     && '["3.11","3.13"]'
                     || '["3.11","3.12","3.13"]') }}

    so the FIRST array is what a pull request runs and the SECOND is what every
    other event runs. Parsed rather than assumed: if someone rewrites this as an
    unconditional list, the assertions below say so instead of reading a stale
    literal out of a comment.
    """
    raw = _matrix_raw(path)
    assert isinstance(raw, str) and "${{" in raw, (
        f"{path.name}: expected an event-conditional matrix expression, got "
        f"{raw!r}. The PR gate must run a NARROWER set than a branch push; an "
        "unconditional list means either every PR pays for the full matrix "
        "again, or `develop` silently lost coverage."
    )
    assert "github.event_name == 'pull_request'" in raw, (
        f"{path.name}: the matrix condition is {raw!r}. It must key on "
        "`github.event_name == 'pull_request'` -- keying on the branch or the "
        "ref would narrow the matrix on `develop` pushes too, which is where "
        "the version the PR gate skips is supposed to be caught."
    )
    arrays = _JSON_ARRAY_RE.findall(raw)
    assert len(arrays) == 2, (
        f"{path.name}: expected exactly two quoted version lists in the matrix "
        f"expression, found {len(arrays)} in {raw!r}."
    )
    pr_list = yaml.safe_load(arrays[0])
    full_list = yaml.safe_load(arrays[1])
    return _sorted(pr_list), _sorted(full_list)


def _declared_versions() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found = []
    for classifier in data["project"]["classifiers"]:
        match = _CLASSIFIER_RE.match(classifier)
        if match:
            found.append(match.group(1))
    return _sorted(found)


PR_VERSIONS, FULL_VERSIONS = _conditional_matrix(PR_GATE_WORKFLOW)
NIGHTLY_VERSIONS = _sorted(_matrix_raw(NIGHTLY_WORKFLOW))
RELEASE_VERSIONS = _sorted(_matrix_raw(RELEASE_WORKFLOW))
DECLARED_VERSIONS = _declared_versions()


def test_parsing_found_real_version_lists():
    """Guard the guard: empty lists would make everything below vacuous."""
    # Arrange
    parsed = {
        "pr": PR_VERSIONS,
        "full": FULL_VERSIONS,
        "nightly": NIGHTLY_VERSIONS,
        "release": RELEASE_VERSIONS,
        "declared": DECLARED_VERSIONS,
    }

    # Act
    empty = [name for name, versions in parsed.items() if not versions]

    # Assert
    assert not empty, (
        f"parsed no python versions for {empty} -- the workflow shape moved and "
        f"every assertion in this module would now pass without checking "
        f"anything. Parsed: {parsed}"
    )


def test_pr_gate_runs_exactly_the_oldest_and_newest_supported():
    """min + max, DERIVED. Bump the full set and forget this list -> red."""
    # Arrange
    expected = [FULL_VERSIONS[0], FULL_VERSIONS[-1]]

    # Act
    actual = PR_VERSIONS

    # Assert
    assert actual == expected, (
        f"the pull-request matrix is {actual}, but the ends of the supported "
        f"range are {expected}. The gate must BRACKET the range: scitex-dev "
        "#578 (LD_LIBRARY_PATH stripped from child processes, 21 tests) failed "
        "on the oldest and newest and PASSED on the middle, because only the "
        "ends link libpython dynamically. Testing an interior version alone "
        "would have shipped it."
    )


def test_nightly_runs_exactly_what_the_pr_gate_skips():
    """The nightly is the COMPLEMENT, so every supported version is tested
    daily by one gate or the other."""
    # Arrange
    expected = [v for v in FULL_VERSIONS if v not in PR_VERSIONS]

    # Act
    actual = NIGHTLY_VERSIONS

    # Assert
    assert actual == expected, (
        f"{NIGHTLY_WORKFLOW.name} runs {actual}; the versions the PR gate does "
        f"not run are {expected}. A version in neither list is tested only on "
        "`develop`/`main` pushes and at release -- i.e. it stops being covered "
        "the moment merges pause, which is exactly when a release is cut."
    )


def test_release_runs_the_full_supported_set():
    # Arrange
    expected = FULL_VERSIONS

    # Act
    actual = RELEASE_VERSIONS

    # Assert
    assert actual == expected, (
        f"{RELEASE_WORKFLOW.name} runs {actual}, not the full supported set "
        f"{expected}. The release gate is the last thing between a "
        "version-specific regression and PyPI; it does not get to inherit the "
        "PR gate's latency trade."
    )


def test_release_matrix_carries_no_event_condition():
    """The release must not be able to inherit a narrowed list by reference or
    by condition -- the half-applied shape this fleet keeps getting caught by."""
    # Arrange
    raw = _matrix_raw(RELEASE_WORKFLOW)

    # Act
    is_plain_literal = isinstance(raw, list) and not any(
        "${{" in str(item) for item in raw
    )

    # Assert
    assert is_plain_literal, (
        f"{RELEASE_WORKFLOW.name}: the release matrix is {raw!r}. It must be a "
        "plain literal list. An expression here could evaluate to the PR gate's "
        "narrowed set on some event nobody thought about, and a release that "
        "quietly tested two of three versions is indistinguishable from one "
        "that tested all three -- until a user hits it."
    )


@pytest.mark.parametrize("version", FULL_VERSIONS, ids=lambda v: v)
def test_ci_never_tests_an_undeclared_version(version: str):
    """CI must not test a python this package does not claim to support."""
    # Arrange
    declared = DECLARED_VERSIONS

    # Act
    is_declared = version in declared

    # Assert
    assert is_declared, (
        f"CI runs python {version} but pyproject.toml declares only {declared}. "
        "Either add the classifier or drop the leg -- a green leg on an "
        "undeclared version is coverage nobody has promised to keep."
    )


def test_the_newest_declared_version_is_on_the_pr_gate():
    """A ceiling bump must reach the gate a human reads, not just the nightly.

    This is the half-application that would otherwise be invisible: add a 3.14
    classifier, add it to the full matrix, and without this assertion the newest
    interpreter our users are told to use would be tested nightly at best.
    """
    # Arrange
    newest_declared = DECLARED_VERSIONS[-1]

    # Act
    on_pr_gate = newest_declared in PR_VERSIONS

    # Assert
    assert on_pr_gate, (
        f"pyproject declares support up to python {newest_declared}, but the "
        f"pull-request gate runs {PR_VERSIONS}. The newest version we claim to "
        "support is the one most likely to break and the one least likely to be "
        "noticed at merge time."
    )


def test_pr_gate_job_names_match_the_required_status_checks():
    """BRANCH PROTECTION IS COUPLED TO THIS MATRIX, and nothing in the repo can
    see that coupling.

    ``develop`` and ``main`` require the check names this job template produces.
    A leg that no longer runs on a pull request NEVER REPORTS, and the PR then
    sits at mergeStateStatus=BLOCKED with every check passing -- which has
    already held a release hostage once in this repo. So narrowing the PR matrix
    REQUIRES editing both protection rules in the same change, from three
    contexts to two::

        pytest-matrix-on-ubuntu-py3.11
        pytest-matrix-on-ubuntu-py3.13

    This test cannot reach the GitHub API, so it pins the other half: the name
    template must still be derived from ``matrix.python-version``, so the
    contexts above remain the ones this matrix actually produces.
    """
    # Arrange
    job = _load(PR_GATE_WORKFLOW)["jobs"]["test"]

    # Act
    name = str(job.get("name", ""))

    # Assert
    assert "matrix.python-version" in name, (
        f"the PR gate job name is {name!r}. It must interpolate "
        "matrix.python-version: branch protection on `develop` and `main` "
        "requires one context per leg, and those context strings are generated "
        "from this template. Renaming it silently orphans the required checks."
    )
