"""``develop`` must never carry unreleased work under an already-spent version.

MEASURED INCIDENT, TWICE, THEN A THIRD TIME THE SAME DAY.

`sac image bake-remote` and every `pip`/`uv` install path key their build-wheel
cache on ``(name, version)`` — not on content. So when the checkout moves
forward while the version string stands still, ``--force-reinstall`` is free to
hand back the *previous* wheel for that same number, and it does. The install
reports success, the version string agrees with what was asked for, and the
bytes are the old ones. **Only the bytes can tell the two apart.**

    v0.21.22  "stop the version lie (21 PRs shipped under a spent number)"

    v0.22.1   #771's srun fix never reached the machine. The installed wheel
              still held pre-#771 bytes — ``grep -c -- --input=none`` returned
              0, all three srun calls unguarded. Its version read 0.22.0, and
              so did the checkout, "because the version was never bumped when
              #771 merged."

Both were repaired by bumping the number by hand. Neither left anything behind
that would notice a third time, and the third time arrived hours later: PR #782
merged 1691 lines and a new ``[codex]`` extra onto ``develop`` while
``pyproject.toml`` still read ``0.22.1`` — a version already published to PyPI.

This module is that missing something. It is deliberately file-only: no
network, no git, no tags, so it cannot flake on the GPFS-backed runners that
have been dropping ``_work/_temp`` files out from under checkout. It reads the
two files that already encode the claim and refuses the one combination that
has burned this repo three times — pending work under the CHANGELOG's
``[Unreleased]`` heading while ``pyproject.toml`` still names a version the
CHANGELOG has already shipped.

The controls matter as much as the check. A guard that parses nothing passes
everything, so the parser is proven to find both operands in the real files,
and the predicate is exercised against synthetic pairs reproducing the 0.22.1
state — through the same function the live assertion calls. A check that has
never been observed to go RED is a hope with a docstring on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_RELEASED_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_UNRELEASED_HEADING = re.compile(
    r"^##\s*\[Unreleased\]\s*$", re.MULTILINE | re.IGNORECASE
)
_ANY_HEADING = re.compile(r"^##\s", re.MULTILINE)

_SEMVER = re.compile(r"\d+\.\d+\.\d+")

_CHANGELOG_WITH_PENDING = """# Changelog

## [Unreleased]

### Added

- Add `spec.claude.provider: codex`.

## [0.22.1] - 2026-07-19

### Fixed

- Something already shipped.
"""

_CHANGELOG_NOTHING_PENDING = """# Changelog

## [Unreleased]

## [0.22.1] - 2026-07-19

### Fixed

- Something already shipped.
"""


def _parse_version(raw: str) -> tuple[int, ...]:
    """``"0.22.1"`` -> ``(0, 22, 1)``. Local, so this test adds no dependency."""
    return tuple(int(part) for part in raw.split(".")[:3])


def declared_version(pyproject_text: str) -> str:
    """The version `pip`/`uv` will key their wheel cache on."""
    match = _PYPROJECT_VERSION.search(pyproject_text)
    assert match is not None, "pyproject.toml declares no top-level version"
    return match.group(1)


def newest_released_version(changelog_text: str) -> str:
    """The newest version the CHANGELOG says has already shipped."""
    match = _RELEASED_HEADING.search(changelog_text)
    assert match is not None, "CHANGELOG.md contains no released `## [X.Y.Z]` heading"
    return match.group(1)


def unreleased_body(changelog_text: str) -> str:
    """Text between ``## [Unreleased]`` and the next ``##`` heading."""
    start = _UNRELEASED_HEADING.search(changelog_text)
    if start is None:
        return ""
    rest = changelog_text[start.end() :]
    nxt = _ANY_HEADING.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def spent_version_violation(pyproject_text: str, changelog_text: str) -> str | None:
    """Return a human-readable violation, or ``None`` when the pair is sound.

    The live assertion and every control call THIS function, so a control that
    goes red is evidence about the check that actually runs.
    """
    declared = declared_version(pyproject_text)
    released = newest_released_version(changelog_text)
    pending = unreleased_body(changelog_text)

    if not pending:
        return None

    if _parse_version(declared) <= _parse_version(released):
        return (
            f"pyproject.toml declares version {declared}, but CHANGELOG.md "
            f"already records {released} as released AND lists pending work "
            f"under [Unreleased]. Installs key their wheel cache on "
            f"(name, version), so this checkout can be served the published "
            f"{released} wheel, report success, and ship none of the pending "
            f"work. Bump the version."
        )
    return None


def test_develop_does_not_ship_pending_work_under_a_spent_version():
    """The live check, against the files as they stand in this checkout."""
    # Arrange
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    # Act
    violation = spent_version_violation(pyproject_text, changelog_text)
    # Assert
    assert violation is None, violation


def test_control_rejects_the_state_develop_was_left_in_by_pr_782():
    """The exact incident state must be REJECTED, not merely disliked."""
    # Arrange
    pyproject_text = 'version = "0.22.1"\n'
    # Act
    violation = spent_version_violation(pyproject_text, _CHANGELOG_WITH_PENDING)
    # Assert
    assert violation is not None


def test_control_names_the_spent_version_in_its_message():
    """The failure has to say WHICH number is spent, or nobody can act on it."""
    # Arrange
    pyproject_text = 'version = "0.22.1"\n'
    # Act
    violation = spent_version_violation(pyproject_text, _CHANGELOG_WITH_PENDING)
    # Assert
    assert "0.22.1" in (violation or "")


def test_control_accepts_the_same_pending_work_under_a_bumped_version():
    """Bumping the number is the whole remedy, so it must clear the check."""
    # Arrange
    pyproject_text = 'version = "0.23.0"\n'
    # Act
    violation = spent_version_violation(pyproject_text, _CHANGELOG_WITH_PENDING)
    # Assert
    assert violation is None


def test_control_accepts_a_clean_slate_immediately_after_a_release():
    """With nothing pending, no bump is due and the check must stay quiet."""
    # Arrange
    pyproject_text = 'version = "0.22.1"\n'
    # Act
    violation = spent_version_violation(pyproject_text, _CHANGELOG_NOTHING_PENDING)
    # Assert
    assert violation is None


def test_control_parser_finds_a_real_version_in_pyproject():
    """A parser that silently finds nothing would pass the live check vacuously."""
    # Arrange
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    # Act
    declared = declared_version(pyproject_text)
    # Assert
    assert _SEMVER.fullmatch(declared), declared


def test_control_parser_finds_a_real_released_version_in_the_changelog():
    """The other operand must be real too, for the same reason."""
    # Arrange
    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    # Act
    released = newest_released_version(changelog_text)
    # Assert
    assert _SEMVER.fullmatch(released), released


@pytest.mark.parametrize(
    "pyproject_text",
    ['name = "scitex-agent-container"\n', ""],
    ids=["no-version-key", "empty"],
)
def test_control_missing_version_is_loud_not_silently_absent(pyproject_text):
    """Absence must raise, never be read as 'no violation'."""
    # Arrange
    parse = declared_version
    # Act
    # Assert
    with pytest.raises(AssertionError):
        parse(pyproject_text)
