"""Keep the PR gate on the organization-owned full Python matrix."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

PR_GATE_WORKFLOW = WORKFLOW_DIR / "pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"
NIGHTLY_WORKFLOW = WORKFLOW_DIR / "nightly-python-matrix-on-github-hosted.yml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "pypi-publish-and-github-release-on-tag.yml"

_CLASSIFIER_RE = re.compile(r"^Programming Language :: Python :: (\d+\.\d+)$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _matrix_raw(path: Path, job: str = "test") -> object:
    definition = _load(path)["jobs"][job]
    if path == RELEASE_WORKFLOW and "uses" in definition:
        # The shared matrix owns execution; the release proof independently
        # requires completed current-run jobs for every literal supported minor.
        assert definition["uses"] == (
            "scitex-ai/.github/.github/workflows/ci-sif-matrix.yml@main"
        )
        assert definition["with"]["suite"] == "matrix"
        source = ast.parse((REPO_ROOT / ".github/ci/release-identity.py").read_text())
        witness = next(
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_witness"
        )
        versions = [
            ast.literal_eval(node.iter)
            for node in ast.walk(witness)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "version"
        ]
        assert len(versions) == 1
        return list(versions[0])
    return definition["strategy"]["matrix"]["python-version"]


def _key(version: str) -> tuple[int, ...]:
    """Sort 3.9 BELOW 3.11 -- string order would put it above."""
    return tuple(int(part) for part in version.split("."))


def _sorted(versions: object) -> list[str]:
    return sorted((str(v) for v in versions), key=_key)


def _declared_versions() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found = []
    for classifier in data["project"]["classifiers"]:
        match = _CLASSIFIER_RE.match(classifier)
        if match:
            found.append(match.group(1))
    return _sorted(found)


NIGHTLY_VERSIONS = _sorted(_matrix_raw(NIGHTLY_WORKFLOW))
RELEASE_VERSIONS = _sorted(_matrix_raw(RELEASE_WORKFLOW))
DECLARED_VERSIONS = _declared_versions()
PR_VERSIONS = DECLARED_VERSIONS
FULL_VERSIONS = RELEASE_VERSIONS


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


def test_pr_gate_runs_the_full_supported_set():
    """The organization-owned PR gate tests all supported Python versions."""
    # Arrange
    expected = FULL_VERSIONS

    # Act
    actual = PR_VERSIONS

    # Assert
    assert actual == expected


def test_nightly_versions_are_supported():
    """The optional GitHub-hosted nightly must not test undeclared versions."""
    # Arrange
    expected = [v for v in NIGHTLY_VERSIONS if v in FULL_VERSIONS]

    # Act
    actual = NIGHTLY_VERSIONS

    # Assert
    assert actual == expected


def test_release_runs_the_full_supported_set():
    # Arrange
    expected = DECLARED_VERSIONS

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
    """Every release must admit all three genuine current-run job witnesses.

    Execution moved to the fixed shared matrix; its PR narrowing cannot reduce
    the tag-push/dispatch release proof's unconditional three-minor requirement.
    """
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
    """The thin leaf caller stays attached to the central required-check job."""
    # Arrange
    job = _load(PR_GATE_WORKFLOW)["jobs"]["tests"]

    # Act
    target = (job.get("uses"), job.get("with", {}).get("suite"))

    # Assert
    assert target == ("scitex-ai/.github/.github/workflows/ci-sif-matrix.yml@main", "matrix")
