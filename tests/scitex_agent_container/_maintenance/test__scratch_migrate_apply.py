"""Applying the ADR-0024 sweep: copy → VERIFY → remove, in that order.

Split from ``test__scratch_migrate.py`` (planning) because these are the only
tests in the suite that DELETE anything, and the order they pin is the whole
safety argument: the overlay copy is removed only after ``verify_copy``
compares every path, size and symlink target against the source. A failure
anywhere before that leaves the copy exactly where it was — measured here by
reading the file back off disk, not by trusting the returned message.

The dry-run half is here too, for the same reason: "the plan writes nothing"
is a claim about the filesystem, so it is asserted against the filesystem.

Real specs, the real loader, real trees, the real ``copytree`` and the real
``rmtree`` (PA-306). Only liveness is injected, through the module's
documented seam — a test cannot make a real agent run.
STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._maintenance._scratch_migrate import (
    apply_scratch_migration,
    move_uvwork,
    plan_scratch_migration,
)
from scitex_agent_container._state.host_scratch import ScratchRoot
from tests.scitex_agent_container._helpers.scratch_fleet import RUNNING, STOPPED
from tests.scitex_agent_container._helpers.scratch_fleet import (
    write_scratch_agent as _write_agent,
)


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    """An empty tmp fleet roster."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    return agents_dir


@pytest.fixture
def scratch(tmp_path: Path) -> ScratchRoot:
    """A resolved scratch root standing in for this host's ``/scratch``."""
    root = tmp_path / "scratch"
    root.mkdir()
    return ScratchRoot(root=root, source="config", reason="test config declares it")


# ---------------------------------------------------------------------------
# The plan writes NOTHING
# ---------------------------------------------------------------------------


def test_planning_leaves_the_overlay_copy_in_place(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    # Act
    plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_planning_does_not_create_the_destination(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    dest = scratch.root / "sac/agents/alpha/uvwork"
    # Act
    plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Applying — copy, verify, THEN remove
# ---------------------------------------------------------------------------


def test_apply_reports_the_move_as_done(fleet: Path, scratch: ScratchRoot) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Act
    results = apply_scratch_migration(plan)
    # Assert
    assert [r.moved for r in results] == [True]


def test_apply_writes_the_tree_onto_scratch(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Act
    apply_scratch_migration(plan)
    # Assert
    dest = scratch.root / "sac/agents/alpha/uvwork/bin/uv"
    assert dest.read_text(encoding="utf-8") == "payload"


def test_apply_removes_the_overlay_copy(fleet: Path, scratch: ScratchRoot) -> None:
    # Arrange — this is the whole point: the root LV gets its space back.
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Act
    apply_scratch_migration(plan)
    # Assert
    assert not source.exists()


def test_apply_preserves_a_symlink_as_a_symlink(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — a venv is mostly symlinks; dereferencing them would both
    # inflate the copy and break the interpreter.
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    (source / "bin" / "python").symlink_to("uv")
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Act
    apply_scratch_migration(plan)
    # Assert
    assert (scratch.root / "sac/agents/alpha/uvwork/bin/python").is_symlink()


def test_apply_leaves_a_refused_agents_overlay_copy_alone(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — a running agent is in the plan, just not in ``movable``.
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=RUNNING)
    # Act
    apply_scratch_migration(plan)
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_a_failed_verification_keeps_the_overlay_copy(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — plan the move, then grow the source so the copy taken from
    # it can no longer match: verification must veto the delete.
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    row = plan.movable[0]
    dest = row.dest
    dest.mkdir(parents=True)
    (dest / "stowaway").write_text("not in the source", encoding="utf-8")
    # Act
    result = move_uvwork(row)
    # Assert
    assert result.moved is False


def test_the_failed_move_says_the_overlay_copy_was_kept(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    row = plan.movable[0]
    row.dest.mkdir(parents=True)
    (row.dest / "stowaway").write_text("not in the source", encoding="utf-8")
    # Act
    result = move_uvwork(row)
    # Assert
    assert "KEPT" in result.detail


def test_a_failed_move_really_leaves_the_source_on_disk(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — the message and the disk must agree.
    source = _write_agent(fleet, "alpha", uvwork={"bin/uv": "payload"})
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    row = plan.movable[0]
    row.dest.mkdir(parents=True)
    (row.dest / "stowaway").write_text("not in the source", encoding="utf-8")
    # Act
    move_uvwork(row)
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"
