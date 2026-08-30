"""The test suite must never touch the operator's real sac state.

These pin the contract of the ``_isolate_state_db`` guard in
``tests/conftest.py``. They are cheap, and they fail LOUDLY if someone
removes the guard or widens its scope — which is worth a file of its own,
because the last time this contract broke it cost a release (ghost tag
v0.21.18) and, in a neighbouring namespace, nearly restarted the live
control plane.

WHY ``claim_port`` USED TO BE THE CANARY, AND WHY IT CANNOT BE ANY MORE.
``a2a_ports.name`` was the PRIMARY KEY of a local table and
``port_allocator.claim_port`` consulted ONLY that database — it never checked
whether a port was really bound. So every distinct agent name that reached
``agent_start`` burned another port out of the fixed range [19000, 19999], and
nothing handed one back inside a run (``release_port`` is only ever called
from ``agent_stop``). Share one database across a ~4900-test session and the
range was gone long before the session was; every later ``agent_start`` then
died with ``RuntimeError: no free a2a port in range [19000, 19999] (all
claimed)``. That is exactly what killed the v0.21.18 release run.

``a2a_ports`` moved to PostgreSQL on 2026-08-28, so ``claim_port`` no longer
touches ``state.db`` at all. Left as written, these two tests would have gone
on passing while measuring a DIFFERENT database's isolation — which is the
precise shape of the vacuous test this file exists to prevent, one level up.

THE CANARY THEREFORE MOVES, AND STAYS ON THE LOCAL FILE. ``record_instance_start``
writes the ``instances`` table, which moved to the shared PostgreSQL
store on 2026-08-28 — so these two take ``pg_schema``, whose throwaway
schema is what now supplies the isolation the temp file used to. The
claim is unchanged and so is the hazard: a leaked row costs a wrong count,
property under test is unchanged: a SECOND test seeing ZERO rows can only
happen if it received a database of its own. The exhaustible-range argument
above does not carry over — a leaked ``instances`` row costs a wrong count
rather than a dead release — so the canary is now weaker in CONSEQUENCE while
testing exactly the same guard. Recorded rather than quietly swapped.

The guard must be FUNCTION-scoped. A session- or module-scoped
"optimisation" would reintroduce the bug silently, which is what
``test_a_second_test_also_starts_from_an_empty_instances_table`` below makes
impossible.
"""

from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_instances import (
    list_active_instances,
    record_instance_start,
)

_REAL_DB = Path("~/.scitex/agent-container/runtime/state.db").expanduser()


def test_default_db_path_is_not_the_real_state_db() -> None:
    """The resolved default must never be the operator's live fleet database."""
    # Arrange
    real = _REAL_DB
    # Act
    resolved = Path(state_db.DEFAULT_DB_PATH)
    # Assert
    assert resolved != real


def test_state_db_env_var_is_not_the_real_state_db() -> None:
    """Subprocesses inherit the env, so the env var must be redirected too."""
    # Arrange
    real = str(_REAL_DB)
    # Act
    env_value = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB", "")
    # Assert
    assert env_value != real


def test_a_fresh_state_db_starts_with_an_empty_instances_table(
    pg_schema: str,
) -> None:
    """A fresh state.db holds no rows, so this test's own write is the only one."""
    # Arrange
    record_instance_start("isolation-probe-a", host="isolation-host")
    # Act
    active = list_active_instances(host="isolation-host")
    # Assert
    assert len(active) == 1


def test_a_second_test_also_starts_from_an_empty_instances_table(
    pg_schema: str,
) -> None:
    """FUNCTION-scope canary — the whole release fix rests on this.

    A DIFFERENT agent name in a DIFFERENT test still sees exactly ONE active
    row. That can only be true if this test received its own database: were
    the guard session- or module-scoped, the previous test's
    ``isolation-probe-a`` row would still be there and the count would be 2.
    Widen the scope and this test goes red immediately, rather than the next
    release going silently missing from PyPI.
    """
    # Arrange
    record_instance_start("isolation-probe-b", host="isolation-host")
    # Act
    active = list_active_instances(host="isolation-host")
    # Assert
    assert len(active) == 1
