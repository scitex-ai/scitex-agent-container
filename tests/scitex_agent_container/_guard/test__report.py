"""The answer shape must make the dangerous report UNBUILDABLE.

Constitution §2 asks for a validator "so a malformed answer fails where it
is built, not three layers downstream". These tests are that validator's
proof: the collapse this guard exists to prevent — an unknown wearing the
word ``clean`` — must raise at construction, not merely be discouraged in
review.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._guard import (
    CLEAN,
    EXIT_CLEAN,
    EXIT_UNDETERMINED,
    EXIT_VIOLATIONS,
    UNDETERMINED,
    VIOLATIONS,
    Deletion,
    DeletionReport,
)

_D = Deletion(path="transforms.py", symbol="class:Scaler",
              first_line=4, last_line=9)


def test_clean_report_exits_zero() -> None:
    """The only verdict that exits 0."""
    # Arrange
    report = DeletionReport(verdict=CLEAN, baseline="git ref HEAD",
                            target="working tree")
    # Act
    code = report.exit_code
    # Assert
    assert code == EXIT_CLEAN


def test_violations_report_exits_three() -> None:
    """3, not 1 — 1 is every framework's generic failure."""
    # Arrange
    report = DeletionReport(verdict=VIOLATIONS, baseline="b", target="t",
                            deletions=(_D,))
    # Act
    code = report.exit_code
    # Assert
    assert code == EXIT_VIOLATIONS


def test_undetermined_report_exits_four() -> None:
    """4 — distinguishable from both clean and violations."""
    # Arrange
    report = DeletionReport(verdict=UNDETERMINED, baseline="b", target="t",
                            undetermined_reason="no baseline was given")
    # Act
    code = report.exit_code
    # Assert
    assert code == EXIT_UNDETERMINED


def test_clean_verdict_cannot_carry_deletions() -> None:
    """The exact collapse the three-valued verdict exists to prevent."""
    # Arrange
    kwargs = {"verdict": CLEAN, "baseline": "b", "target": "t",
              "deletions": (_D,)}
    # Act
    act = lambda: DeletionReport(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="carrying deletions"):
        act()


def test_clean_verdict_cannot_carry_unparsable_files() -> None:
    """Their symbols were never compared, so 'clean' is unprovable."""
    # Arrange
    kwargs = {"verdict": CLEAN, "baseline": "b", "target": "t",
              "broken_files": ("transforms.py",)}
    # Act
    act = lambda: DeletionReport(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="never compared"):
        act()


def test_undetermined_verdict_requires_a_reason() -> None:
    """An unexplained unknown is indistinguishable from a bug."""
    # Arrange
    kwargs = {"verdict": UNDETERMINED, "baseline": "b", "target": "t"}
    # Act
    act = lambda: DeletionReport(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="must state WHY"):
        act()


def test_violations_verdict_requires_a_finding() -> None:
    """A guard must say WHAT was deleted, not merely that something was."""
    # Arrange
    kwargs = {"verdict": VIOLATIONS, "baseline": "b", "target": "t"}
    # Act
    act = lambda: DeletionReport(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="nothing to name"):
        act()


def test_unknown_verdict_string_is_rejected() -> None:
    """A typo'd verdict must fail at construction, not render as prose."""
    # Arrange
    kwargs = {"verdict": "ok", "baseline": "b", "target": "t"}
    # Act
    act = lambda: DeletionReport(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="is not one of"):
        act()


def test_json_shape_has_every_declared_key() -> None:
    """The JSON contract is stable: keys never disappear, values may empty."""
    # Arrange
    report = DeletionReport(verdict=CLEAN, baseline="b", target="t")
    # Act
    keys = set(report.to_dict())
    # Assert
    assert keys == {
        "verdict", "exit_code", "baseline", "target", "files_compared",
        "deletions", "deleted_files", "broken_files", "allowed_deletions",
        "undetermined_reason", "next_steps",
    }


def test_json_deletion_entry_shape_is_stable() -> None:
    """Each deletion carries path, symbol, line span and its allow key."""
    # Arrange
    report = DeletionReport(verdict=VIOLATIONS, baseline="b", target="t",
                            deletions=(_D,))
    # Act
    keys = set(report.to_dict()["deletions"][0])
    # Assert
    assert keys == {"path", "symbol", "first_line", "last_line", "key"}


def test_deletion_key_is_the_allow_token() -> None:
    """`--allow` takes exactly what the report printed."""
    # Arrange
    deletion = _D
    # Act
    key = deletion.key
    # Assert
    assert key == "transforms.py::class:Scaler"


def test_deletion_where_names_the_line_span() -> None:
    """'file:4-9' is what makes the error actionable."""
    # Arrange
    deletion = _D
    # Act
    where = deletion.where
    # Assert
    assert where == "transforms.py:4-9"
