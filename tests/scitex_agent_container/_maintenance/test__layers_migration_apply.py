"""Tests for the transactional to_home_layers apply.

The behaviour that matters is what happens when verification FAILS after the
writes: every original must come back, and the result must say it rolled back
rather than that it refused. A partially-applied sweep is the one outcome this
must never produce.

STX-NM002: no mocks — real files under tmp_path, and the verifier is a
hand-rolled object with the same two attributes the real one exposes.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._maintenance._layers_migration_apply import (
    apply_migration,
)
from scitex_agent_container._maintenance._layers_migration_model import (
    MigrationPlan,
    SpecEdit,
)

_ORIGINAL = "spec:\n  to_home: ./to_home\n"
_MIGRATED = "spec:\n  to_home: ./to_home\n  to_home_layers: [user-shared]\n"


class _Verdict:
    """The two attributes ``apply_migration`` reads off a verifier."""

    def __init__(self, safe: bool) -> None:
        self.safe = safe

    def summary(self) -> str:
        return "safe" if self.safe else "1 agent(s) LOST hooks"


def _plan(tmp_path: Path, agents=("a", "b")) -> MigrationPlan:
    edits = []
    for name in agents:
        p = tmp_path / f"{name}.yaml"
        p.write_text(_ORIGINAL)
        edits.append(
            SpecEdit(
                agent=name,
                path=p,
                layers=("user-shared",),
                new_text=_MIGRATED,
                lines_added=1,
            )
        )
    return MigrationPlan(edits=tuple(edits))


def test_a_verified_apply_writes_the_specs(tmp_path: Path) -> None:
    # Arrange
    plan = _plan(tmp_path)
    # Act
    apply_migration(plan, tmp_path / "archive", lambda: _Verdict(True))
    # Assert
    assert (tmp_path / "a.yaml").read_text() == _MIGRATED


def test_a_verified_apply_reports_applied(tmp_path: Path) -> None:
    # Arrange
    plan = _plan(tmp_path)
    # Act
    result = apply_migration(plan, tmp_path / "archive", lambda: _Verdict(True))
    # Assert
    assert result.applied is True


def test_a_failed_verification_restores_every_original(tmp_path: Path) -> None:
    # Arrange — the whole reason the archive is written before the first byte.
    plan = _plan(tmp_path)
    # Act
    apply_migration(plan, tmp_path / "archive", lambda: _Verdict(False))
    # Assert
    assert [(tmp_path / f"{n}.yaml").read_text() for n in ("a", "b")] == [
        _ORIGINAL,
        _ORIGINAL,
    ]


def test_a_failed_verification_reports_rolled_back(tmp_path: Path) -> None:
    # Arrange
    plan = _plan(tmp_path)
    # Act
    result = apply_migration(plan, tmp_path / "archive", lambda: _Verdict(False))
    # Assert
    assert result.rolled_back is not None


def test_a_rollback_is_not_reported_as_a_refusal(tmp_path: Path) -> None:
    # Arrange — refusing BEFORE writing and undoing AFTER leave the filesystem
    # identical but mean very different things.
    plan = _plan(tmp_path)
    # Act
    result = apply_migration(plan, tmp_path / "archive", lambda: _Verdict(False))
    # Assert
    assert result.refused is None


def test_a_rollback_is_not_applied(tmp_path: Path) -> None:
    # Arrange
    plan = _plan(tmp_path)
    # Act
    result = apply_migration(plan, tmp_path / "archive", lambda: _Verdict(False))
    # Assert
    assert result.applied is False


def test_the_rollback_message_carries_the_verifier_detail(tmp_path: Path) -> None:
    # Arrange — an operator must learn WHY, not just that it undid itself.
    plan = _plan(tmp_path)
    # Act
    result = apply_migration(plan, tmp_path / "archive", lambda: _Verdict(False))
    # Assert
    assert "LOST hooks" in (result.rolled_back or "")


def test_an_unsafe_plan_is_refused_before_writing(tmp_path: Path) -> None:
    # Arrange — a malformed edit means the plan does not describe reality.
    plan = _plan(tmp_path)
    bad = MigrationPlan(
        edits=tuple(
            SpecEdit(
                agent=e.agent,
                path=e.path,
                layers=e.layers,
                new_text=e.new_text,
                lines_added=4,
            )
            for e in plan.edits
        )
    )
    # Act
    apply_migration(bad, tmp_path / "archive", lambda: _Verdict(True))
    # Assert
    assert (tmp_path / "a.yaml").read_text() == _ORIGINAL


def test_an_unsafe_plan_reports_refused(tmp_path: Path) -> None:
    # Arrange
    plan = _plan(tmp_path)
    bad = MigrationPlan(
        edits=tuple(
            SpecEdit(
                agent=e.agent,
                path=e.path,
                layers=e.layers,
                new_text=e.new_text,
                lines_added=4,
            )
            for e in plan.edits
        )
    )
    # Act
    result = apply_migration(bad, tmp_path / "archive", lambda: _Verdict(True))
    # Assert
    assert result.refused is not None


def test_a_plan_with_nothing_to_write_is_refused(tmp_path: Path) -> None:
    # Arrange — an empty sweep is a mistake worth naming, not a silent success.
    # Act
    result = apply_migration(
        MigrationPlan(), tmp_path / "archive", lambda: _Verdict(True)
    )
    # Assert
    assert result.refused == "plan would write nothing"


def test_the_archive_keeps_both_specs_despite_shared_basenames(
    tmp_path: Path,
) -> None:
    # Arrange — real specs are all named spec.yaml; a flat copy would keep one.
    a = tmp_path / "agent-a"
    a.mkdir()
    b = tmp_path / "agent-b"
    b.mkdir()
    edits = []
    for name, d in (("a", a), ("b", b)):
        p = d / "spec.yaml"
        p.write_text(_ORIGINAL)
        edits.append(
            SpecEdit(
                agent=name,
                path=p,
                layers=("user-shared",),
                new_text=_MIGRATED,
                lines_added=1,
            )
        )
    plan = MigrationPlan(edits=tuple(edits))
    archive = tmp_path / "archive"
    # Act
    apply_migration(plan, archive, lambda: _Verdict(False))
    # Assert
    assert len(list(archive.iterdir())) == 2
