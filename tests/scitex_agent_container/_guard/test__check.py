"""The guard must fail on the incident, pass a clean feature, and — the
part that matters most — say "I could not tell" out loud.

A GUARD YOU HAVE ONLY EVER SEEN PASS IS A HOPE. So this file proves all
three verdicts against real git repositories, and asserts explicitly that
``could-not-determine`` is NOT ``clean`` rather than merely checking a
non-zero exit: a guard that exits non-zero for the wrong reason is still
lying about what it knows.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._guard import (
    CLEAN,
    EXIT_UNDETERMINED,
    EXIT_VIOLATIONS,
    UNDETERMINED,
    VIOLATIONS,
    check_deletions,
)

from .conftest import TRANSFORMS_CLEAN_FEATURE, TRANSFORMS_INCIDENT


def test_incident_shape_reports_violations(repo: Path) -> None:
    """Adding a function while dropping two classes is a violation."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert report.verdict == VIOLATIONS


def test_incident_names_both_deleted_classes(repo: Path) -> None:
    """The report NAMES what vanished — 'deletions found' is half-written."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    # Act
    keys = {d.key for d in check_deletions(repo=repo, base="HEAD").deletions}
    # Assert
    assert {"transforms.py::class:Scaler",
            "transforms.py::class:Normalizer"} <= keys


def test_incident_carries_baseline_line_numbers(repo: Path) -> None:
    """Every deletion points at WHERE the code used to be."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    report = check_deletions(repo=repo, base="HEAD")
    # Act
    scaler = next(d for d in report.deletions if d.symbol == "class:Scaler")
    # Assert
    assert (scaler.first_line, scaler.last_line) == (4, 9)


def test_incident_exit_code_is_violations(repo: Path) -> None:
    """Violations exit 3 — not 1, which every framework already owns."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert report.exit_code == EXIT_VIOLATIONS


def test_clean_multi_file_feature_is_clean(repo: Path) -> None:
    """Adding across two files while deleting nothing is CLEAN."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_CLEAN_FEATURE)
    (repo / "helpers.py").write_text("def widen(values):\n    return values\n")
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert (report.verdict, report.exit_code) == (CLEAN, 0)


def test_allowed_deletion_is_not_a_violation(repo: Path) -> None:
    """A deletion the task DID require passes when it is declared."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    allowed = ["transforms.py::class:Scaler",
               "transforms.py::class:Normalizer"]
    # Act
    report = check_deletions(repo=repo, base="HEAD", allowed=allowed)
    # Assert
    assert report.verdict == CLEAN


def test_allowing_a_class_allows_its_methods(repo: Path) -> None:
    """One --allow per class, not one per method: a guard must be clearable."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    allowed = ["transforms.py::class:Scaler",
               "transforms.py::class:Normalizer"]
    # Act
    report = check_deletions(repo=repo, base="HEAD", allowed=allowed)
    # Assert
    assert "transforms.py::class:Scaler.apply" in report.allowed_deletions


def test_allowing_one_class_still_flags_the_other(repo: Path) -> None:
    """The expansion is scoped to the named class, not to everything."""
    # Arrange
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    allowed = ["transforms.py::class:Scaler"]
    # Act
    report = check_deletions(repo=repo, base="HEAD", allowed=allowed)
    # Assert
    assert "transforms.py::class:Normalizer" in {
        d.key for d in report.deletions
    }


def test_deleted_file_is_a_violation(repo: Path) -> None:
    """A whole file vanishing counts, not just a symbol inside one."""
    # Arrange
    (repo / "pipeline.py").unlink()
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert "pipeline.py" in report.deleted_files


def test_missing_baseline_is_undetermined(repo: Path) -> None:
    """No --base and no snapshot pair: nothing was compared."""
    # Arrange
    kwargs = {"repo": repo}
    # Act
    report = check_deletions(**kwargs)
    # Assert
    assert report.verdict == UNDETERMINED


def test_missing_baseline_is_not_reported_clean(repo: Path) -> None:
    """THE regression this guard exists for: unknown must never be clean."""
    # Arrange
    kwargs = {"repo": repo}
    # Act
    report = check_deletions(**kwargs)
    # Assert
    assert report.verdict != CLEAN


def test_missing_baseline_exits_nonzero(repo: Path) -> None:
    """Undetermined exits 4 — a distinct, declared, non-zero code."""
    # Arrange
    kwargs = {"repo": repo}
    # Act
    report = check_deletions(**kwargs)
    # Assert
    assert report.exit_code == EXIT_UNDETERMINED


def test_missing_baseline_states_its_reason(repo: Path) -> None:
    """An unexplained unknown is indistinguishable from a bug."""
    # Arrange
    kwargs = {"repo": repo}
    # Act
    report = check_deletions(**kwargs)
    # Assert
    assert "no baseline" in (report.undetermined_reason or "")


def test_unknown_ref_is_undetermined(repo: Path) -> None:
    """An invalid baseline ref cannot be silently treated as empty."""
    # Arrange
    ref = "no-such-ref"
    # Act
    report = check_deletions(repo=repo, base=ref)
    # Assert
    assert report.verdict == UNDETERMINED


def test_unknown_ref_is_not_reported_clean(repo: Path) -> None:
    """An empty tree compares clean against anything — so never do that."""
    # Arrange
    ref = "no-such-ref"
    # Act
    report = check_deletions(repo=repo, base=ref)
    # Assert
    assert report.verdict != CLEAN


def test_non_git_directory_is_undetermined(not_a_repo: Path) -> None:
    """--base against a plain directory has no baseline to read."""
    # Arrange
    ref = "HEAD"
    # Act
    report = check_deletions(repo=not_a_repo, base=ref)
    # Assert
    assert report.verdict == UNDETERMINED


def test_half_a_snapshot_pair_is_undetermined(tmp_path: Path) -> None:
    """--before without --after is not a baseline, it is half of one."""
    # Arrange
    before = tmp_path / "before"
    before.mkdir()
    # Act
    report = check_deletions(before=str(before))
    # Assert
    assert report.verdict == UNDETERMINED


def test_missing_snapshot_dir_is_undetermined(tmp_path: Path) -> None:
    """A missing snapshot directory is not an empty one."""
    # Arrange
    after = tmp_path / "after"
    after.mkdir()
    # Act
    report = check_deletions(before=str(tmp_path / "gone"), after=str(after))
    # Assert
    assert report.verdict == UNDETERMINED


def test_snapshot_pair_detects_the_incident(tmp_path: Path) -> None:
    """The explicit before/after mode finds the same deletions git does."""
    # Arrange
    before, after = tmp_path / "b", tmp_path / "a"
    before.mkdir()
    after.mkdir()
    from .conftest import TRANSFORMS_BEFORE

    (before / "transforms.py").write_text(TRANSFORMS_BEFORE)
    (after / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    # Act
    report = check_deletions(before=str(before), after=str(after))
    # Assert
    assert report.verdict == VIOLATIONS


def test_unparsable_file_is_undetermined(repo: Path) -> None:
    """A file that no longer parses was never compared — say so."""
    # Arrange
    (repo / "transforms.py").write_text("class Scaler(:\n")
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert report.verdict == UNDETERMINED


def test_unparsable_file_is_not_reported_clean(repo: Path) -> None:
    """Its symbols are invisible to the diff, so 'clean' is unprovable."""
    # Arrange
    (repo / "transforms.py").write_text("class Scaler(:\n")
    # Act
    report = check_deletions(repo=repo, base="HEAD")
    # Assert
    assert report.verdict != CLEAN


def test_ref_to_ref_comparison_is_supported(repo: Path) -> None:
    """--target lets the guard compare two commits without a worktree."""
    # Arrange
    import subprocess

    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    for args in (["add", "-A"], ["commit", "-q", "-m", "incident"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    # Act
    report = check_deletions(repo=repo, base="HEAD~1", target="HEAD")
    # Assert
    assert report.verdict == VIOLATIONS
