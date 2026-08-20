"""A test must not be able to write into the live fleet store.

REGRESSION GUARD for a measured incident, 2026-08-20.

WHAT HAPPENED
=============
PR #1154 moved the birth certificate to PostgreSQL and dropped ``db_path``
from ``write_birth_certificate``. Tests that had been threading a
``tmp_path`` SQLite file silently began resolving the FLEET DSN instead.
One full-suite run on scitex-compute-04 wrote **46 rows into the live
``incarnations`` store** — ``alpha``, ``zombie``, ``born-1``..``born-4``,
``pid-*``, ``rec-*``, ``grant-*``, ``screen-*``: every one a fixture name,
all stamped ``spec_git_sha='unresolvable'``, all born inside the seven
minutes the suite was running. They were removed afterwards with the
store's own ``hide`` verb.

WHY A GUARD AND NOT JUST THE FIX
================================
The rows were the small half. sac's CI runs on SELF-HOSTED runners sitting
on the fleet hosts, so this was a test suite scheduled to edit production
state on every push, on whichever machine it landed on. And it was
INVISIBLE: ``write_birth_certificate`` is best-effort, so a test that wrote
to the fleet store passed exactly like one that did not. Nothing in the
suite's output distinguished them — the only symptom was a row count, and
only if someone thought to compare it against SQLite.

That is the shape this file exists to prevent recurring. The conftest
fixture is the fix; without a test, the next person to add an autouse
fixture, reorder conftest, or "simplify" the DSN handling removes the fix
and every check still passes.

WHAT IS ASSERTED
================
Three levels, deliberately: the variable, the resolved target, and an
actual write. The first two could both be satisfied while a caller reached
the fleet store by some other route, so the third is the one that matters
and the first two are there to say WHERE it broke when it breaks.
"""

from __future__ import annotations

import os

from scitex_agent_container._state.state_db_incarnations import (
    get_incarnation,
    incarnation_store_target,
)

#: The fleet's real store. If a test ever resolves this, the guard is gone.
_FLEET_HOST_PORT = "127.0.0.1:55432"


def test_the_store_dsn_is_not_the_fleet_store_during_a_test() -> None:
    # Arrange: the autouse conftest guard has already run for this test.
    dsn = os.environ.get("SCITEX_STORE_DSN", "")
    # Act
    points_at_fleet = _FLEET_HOST_PORT in dsn
    # Assert
    assert not points_at_fleet


def test_the_resolved_target_is_not_the_fleet_store() -> None:
    """The variable being set is not the same as the resolver honouring it.

    Checked separately because ``host_store`` is a two-step resolution: a
    future change that stopped reading ``SCITEX_STORE_DSN`` would leave the
    assertion above green while sending writes back to the fleet.
    """
    # Arrange
    target = incarnation_store_target()
    # Act
    locator = str(target.locator)
    # Assert
    assert _FLEET_HOST_PORT not in locator


def test_a_birth_certificate_written_during_a_test_does_not_land() -> None:
    """The behavioural assertion: the write must not reach a real store.

    Uses the best-effort launch path, which is exactly how the 46 rows were
    written — not the store API directly, because the incident came through
    a caller that swallows failures rather than through a deliberate write.

    ASSERTS ON THE RETURN VALUE, and the first draft did not. It asserted
    ``get_incarnation(...) is None``, which cannot hold: with no store to
    reach, the READ raises exactly as the write did, so the test failed
    while the guard was working perfectly. "Nothing was written" and
    "reading finds nothing" are different claims, and only the first one is
    observable when the store is deliberately absent.
    """
    # Arrange
    from scitex_agent_container._lifecycle._birth_certificate import (
        write_birth_certificate,
    )
    from scitex_agent_container.config import AgentConfig

    cfg = AgentConfig(name="guard-must-not-persist")
    # Act
    landed = write_birth_certificate(cfg, "inc-guard-must-not-persist")
    # Assert: the launch path reports the record did not land.
    assert landed is False


def test_the_guard_lets_a_test_opt_in_to_a_real_store(pg_schema: str) -> None:
    """POSITIVE CONTROL — without it the three tests above are unfalsifiable.

    A guard that blocked EVERY store access would satisfy all of them and
    also break every legitimate PostgreSQL test in the suite. This proves
    the opt-in path still reaches a real database, just not the fleet's.
    """
    # Arrange
    from scitex_agent_container._state.state_db_incarnations import (
        record_incarnation_birth,
    )

    record_incarnation_birth(
        "inc-guard-opt-in",
        agent_id="guard",
        spec_id=None,
        spec_git_sha="unresolvable",
        host="h",
        compiled_spec_json="{}",
    )
    # Act
    row = get_incarnation("inc-guard-opt-in")
    # Assert
    assert row is not None
