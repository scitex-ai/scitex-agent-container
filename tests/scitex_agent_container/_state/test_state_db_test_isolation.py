"""The test suite must never touch the operator's real sac state.

These pin the contract of the ``_isolate_state_db`` guard in
``tests/conftest.py``. They are cheap, and they fail LOUDLY if someone
removes the guard or widens its scope — which is worth a file of its own,
because the last time this contract broke it cost a release (ghost tag
v0.21.18) and, in a neighbouring namespace, nearly restarted the live
control plane.

Why ``claim_port`` is the canary. ``a2a_ports.name`` is the PRIMARY KEY and
:func:`port_allocator.claim_port` consults ONLY the database — it never checks
whether a port is really bound. So every distinct agent name that reaches
``agent_start`` burns another port out of the fixed range [19000, 19999], and
nothing hands one back inside a run (``release_port`` is only ever called from
``agent_stop``). Share one database across a ~4900-test session and the range
is gone long before the session is; every later ``agent_start`` then dies with
``RuntimeError: no free a2a port in range [19000, 19999] (all claimed)``.

That is exactly what killed the v0.21.18 release run, and it is why the guard
must be FUNCTION-scoped. A session- or module-scoped "optimisation" would
reintroduce the bug silently — so ``test_claim_port_starts_at_the_range_floor_
in_a_second_test`` below exists to make that impossible: it can only pass if
this test got a database of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container._state import state_db
from scitex_agent_container._state.port_allocator import claim_port

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


def test_claim_port_starts_at_the_range_floor() -> None:
    """A fresh state.db has an empty ``a2a_ports``, so the scan hands out 19000."""
    # Arrange
    name = "isolation-probe-a"
    # Act
    port = claim_port(name)
    # Assert
    assert port == 19000


def test_claim_port_starts_at_the_range_floor_in_a_second_test() -> None:
    """FUNCTION-scope canary — the whole release fix rests on this.

    A DIFFERENT agent name in a DIFFERENT test still gets 19000. That can only
    be true if this test received its own database: were the guard session- or
    module-scoped, the previous test's ``isolation-probe-a`` row would still be
    there and the allocator would hand out 19001 instead. Widen the scope and
    this test goes red immediately, rather than the next release going silently
    missing from PyPI.
    """
    # Arrange
    name = "isolation-probe-b"
    # Act
    port = claim_port(name)
    # Assert
    assert port == 19000
