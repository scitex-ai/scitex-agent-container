"""Stale-lease cleanup also clears the on-disk incarnation marker.

v4 step 5: a crashed run never reaches ``agent_stop``'s
``clear_instance_id``, so its ``instance_id`` marker lingers in the
state dir. The next runner ADOPTS the first fresh marker it sees at
boot (bind-once — ``_runners._incarnation``), so the start path's
stale-lease sweep — which already runs exactly when the runtime reports
the agent DEAD — must remove it. Real files under the isolated runtime
floor; the instances oracle is a real callable returning no rows.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._stale_lease import clear_stale_instance_lease
from scitex_agent_container._runners._session_state import (
    read_instance_id,
    state_dir_for,
    write_instance_id,
)


def test_sweep_removes_the_stale_incarnation_marker() -> None:
    # Arrange: a previous incarnation's marker with no live rows at all.
    state_dir = state_dir_for("stale-marker-x")
    write_instance_id(state_dir, "inc-stale-1")
    # Act
    clear_stale_instance_lease("stale-marker-x", instances_oracle=lambda: [])
    # Assert
    assert read_instance_id(state_dir) is None


def test_sweep_without_marker_still_returns_zero() -> None:
    # Arrange: nothing on disk, nothing in the table.
    name = "stale-marker-none"
    # Act
    cleared = clear_stale_instance_lease(name, instances_oracle=lambda: [])
    # Assert
    assert cleared == 0
