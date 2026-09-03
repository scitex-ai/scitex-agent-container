"""PLANNING the move of ``overlays/<agent>/upper/uvwork`` onto scratch.

The read-only half of ADR-0024, over a real corpus of real spec files with
real trees on disk. What an operator reads before letting anything delete
11.7 GB is the PLAN, so the properties under test are the ones that decide
whether reading it is worth anything — above all that the not-moving
outcomes stay APART:

* a RUNNING agent is refused; its container has the overlay mounted;
* an UNDETERMINABLE one is refused too — "unknown" is never "stopped";
* an overlay TWO agents declare is refused, naming the others (measured on
  the fleet: one 2.6 GiB tree appeared as movable twice); and
* an agent with nothing under ``/uvwork`` is simply ``nothing``.

Collapsing any two of those is how a sweep deletes a live agent's venv and
calls it housekeeping. Applying, and the "the plan writes nothing" proof,
live in ``test__scratch_migrate_apply.py``; the measuring and liveness
instruments have their own files beside this one.

The liveness probe is injected through the module's documented ``liveness``
parameter — the same seam ``_worktree_gc`` uses for its ``gh`` lookup. That
is not a mock of the thing under test: the thing under test is the PLAN's
arithmetic over a liveness answer, and a test cannot make a real agent run.
Everything else is real (PA-306): real specs, read by the real loader.
STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._maintenance._scratch_migrate import (
    plan_scratch_migration,
)
from scitex_agent_container._state.host_scratch import ScratchRoot
from tests.scitex_agent_container._helpers.scratch_fleet import (
    RUNNING,
    STOPPED,
    UNKNOWN,
)
from tests.scitex_agent_container._helpers.scratch_fleet import row_for as _row
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
# Planning — reads only, and the refusals stay apart
# ---------------------------------------------------------------------------


def test_a_stopped_agent_with_an_overlay_uvwork_is_planned_to_move(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x" * 10})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").action == "move"


def test_the_plan_carries_the_measured_byte_count(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x" * 10})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert plan.total_bytes == 10


def test_the_plan_names_the_destination_on_scratch(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").dest == scratch.root / "sac/agents/alpha/uvwork"


def test_a_running_agent_is_refused(fleet: Path, scratch: ScratchRoot) -> None:
    # Arrange — its container has the overlay mounted RIGHT NOW.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=RUNNING)
    # Assert
    assert _row(plan, "alpha").action == "refuse"


def test_the_running_refusal_names_the_stop_command(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=RUNNING)
    # Assert
    assert "sac agents stop alpha" in _row(plan, "alpha").reason


def test_a_running_agent_contributes_no_bytes_to_the_total(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — the headline number must not promise space held by a
    # refusal.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x" * 10})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=RUNNING)
    # Assert
    assert plan.total_bytes == 0


def test_undeterminable_liveness_is_refused_not_read_as_stopped(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — "the instrument could not answer" must never become "no".
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=UNKNOWN)
    # Assert
    assert _row(plan, "alpha").action == "refuse"


def test_the_undeterminable_refusal_names_the_instrument(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=UNKNOWN)
    # Assert
    assert "ApptainerRuntime.is_running" in _row(plan, "alpha").reason


def test_an_agent_with_no_overlay_uvwork_has_nothing_to_move(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — an overlay, but nothing ever written under /uvwork.
    _write_agent(fleet, "alpha", uvwork=None)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").action == "nothing"


def test_a_spec_with_no_overlay_has_nothing_to_move(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange
    _write_agent(fleet, "alpha", overlay=False)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").reason == "spec declares no overlay"


def test_an_image_overlay_is_not_walked(fleet: Path, scratch: ScratchRoot) -> None:
    # Arrange — a loopback ext3 image's upper is not a host directory.
    _write_agent(fleet, "alpha", overlay_size="4096")
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert "image (loopback) overlay" in _row(plan, "alpha").reason


def test_an_already_populated_destination_is_refused(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — the agent already restarted under the new bind, so the
    # overlay copy is now the OLDER of the two.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "old"})
    dest = scratch.root / "sac/agents/alpha/uvwork"
    dest.mkdir(parents=True)
    (dest / "bin").mkdir()
    (dest / "bin" / "uv").write_text("new", encoding="utf-8")
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").action == "refuse"


def test_an_empty_destination_directory_does_not_block_the_move(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — the positive control: the bind's own mkdir ran, nothing more.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    (scratch.root / "sac/agents/alpha/uvwork").mkdir(parents=True)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert _row(plan, "alpha").action == "move"


def test_only_the_named_agent_is_planned(fleet: Path, scratch: ScratchRoot) -> None:
    # Arrange
    _write_agent(fleet, "alpha", uvwork={"f": "x"})
    _write_agent(fleet, "beta", uvwork={"f": "x"})
    # Act
    plan = plan_scratch_migration(
        scratch, agents_root=fleet, only=["alpha"], liveness=STOPPED
    )
    # Assert
    assert [r.agent for r in plan.rows] == ["alpha"]


def test_an_unknown_named_agent_makes_the_plan_unsafe(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — a plan that cannot describe every selected spec does not
    # describe the sweep.
    _write_agent(fleet, "alpha", uvwork={"f": "x"})
    # Act
    plan = plan_scratch_migration(
        scratch, agents_root=fleet, only=["ghost"], liveness=STOPPED
    )
    # Assert
    assert plan.safe_to_apply is False


def test_an_empty_roster_is_not_a_sound_plan(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — "0 to move" over a roster nobody found is not a fact.
    missing = fleet / "not-a-roster"
    # Act
    plan = plan_scratch_migration(scratch, agents_root=missing, liveness=STOPPED)
    # Assert
    assert plan.safe_to_apply is False


def test_a_host_that_keeps_uvwork_in_the_overlay_cannot_be_planned(
    fleet: Path,
) -> None:
    # Arrange — there is nowhere to migrate TO.
    decided = ScratchRoot(root=None, source="none", reason="root LV is 8T")
    # Act
    raised = pytest.raises(ValueError, match="needs a scratch root")
    # Assert
    with raised:
        plan_scratch_migration(decided, agents_root=fleet, liveness=STOPPED)


# ---------------------------------------------------------------------------
# One overlay, two agents — measured on the fleet, refused by name
# ---------------------------------------------------------------------------


def test_an_overlay_two_agents_declare_is_refused(
    fleet: Path, scratch: ScratchRoot, tmp_path: Path
) -> None:
    # Arrange — the real shape: scitex-hub and scitex-hub-mobile-ux name ONE
    # --overlay, so the sweep saw one 2.6 GiB tree as movable twice.
    shared = tmp_path / "shared-overlay"
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    _write_agent(fleet, "beta", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert [r.action for r in plan.rows] == ["refuse", "refuse"]


def test_the_shared_overlay_refusal_names_the_other_agent(
    fleet: Path, scratch: ScratchRoot, tmp_path: Path
) -> None:
    # Arrange — the operator has to know WHO else reads that tree.
    shared = tmp_path / "shared-overlay"
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    _write_agent(fleet, "beta", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert "beta" in _row(plan, "alpha").reason


def test_a_shared_overlay_contributes_no_bytes_to_the_total(
    fleet: Path, scratch: ScratchRoot, tmp_path: Path
) -> None:
    # Arrange — counting it twice was how one tree became 5.2 GiB of promise.
    shared = tmp_path / "shared-overlay"
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x" * 10}, overlay_dir=shared)
    _write_agent(fleet, "beta", uvwork={"bin/uv": "x" * 10}, overlay_dir=shared)
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert plan.total_bytes == 0


def test_two_agents_with_their_own_overlays_both_move(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — the positive control: the guard must key on the SHARED path,
    # not simply refuse whenever two agents are present.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    _write_agent(fleet, "beta", uvwork={"bin/uv": "x"})
    # Act
    plan = plan_scratch_migration(scratch, agents_root=fleet, liveness=STOPPED)
    # Assert
    assert [r.action for r in plan.rows] == ["move", "move"]


def test_sharing_is_detected_even_when_only_one_agent_is_named(
    fleet: Path, scratch: ScratchRoot, tmp_path: Path
) -> None:
    # Arrange — `--agent alpha` alone must not walk into the shared tree
    # just because the other claimant was filtered out of the plan.
    shared = tmp_path / "shared-overlay"
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    _write_agent(fleet, "beta", uvwork={"bin/uv": "x"}, overlay_dir=shared)
    # Act
    plan = plan_scratch_migration(
        scratch, agents_root=fleet, only=["alpha"], liveness=STOPPED
    )
    # Assert
    assert _row(plan, "alpha").action == "refuse"


def test_an_unselected_unreadable_spec_does_not_make_the_plan_unsafe(
    fleet: Path, scratch: ScratchRoot
) -> None:
    # Arrange — loading every spec for the ownership map must not turn some
    # unrelated broken spec into this invocation's problem.
    _write_agent(fleet, "alpha", uvwork={"bin/uv": "x"})
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("{ not: [valid", encoding="utf-8")
    # Act
    plan = plan_scratch_migration(
        scratch, agents_root=fleet, only=["alpha"], liveness=STOPPED
    )
    # Assert
    assert plan.safe_to_apply is True


