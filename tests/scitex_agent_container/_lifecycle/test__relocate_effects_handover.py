"""The phase and the preflight must reach the same verdict, or the gate is worthless.

HANDOVER is the last phase before DONE. By the time it runs the agent is stopped
on the source, its transcript has been copied and verified, the target has booted
and answered a handshake. So a refusal here is a correct refusal delivered to an
operator whose agent is already down — which is what the 2026-08-11 canary got on
its return leg (exit 5, nothing running anywhere).

The fix was to ask the question earlier. The risk that creates is the one these
tests exist to close: a preflight that PASSES on a phase that will still REFUSE
is worse than no preflight, because it converts a refusal into a surprise. So the
rule lives in ONE function, :func:`._relocate_lease_readiness.handoff_readiness`,
and both call it — and the tests below drive the PHASE against the same states
:mod:`test__relocate_lease_readiness` drives the predicate against.

NOT MOCKED. The lease store is a REAL PostgreSQL one — a throwaway schema from
the shared ``pg_schema`` fixture, since the lease moved stores on 2026-08-28
— and the liveness observation runs the real tmux script through ``sh -c``: for
an agent name no session exists under, tmux answers honestly and that answer is
the machine's, not a canned one. Only the "another host IS running it" case is
supplied as canned marker output, because creating a real second live agent is
not something a unit test may do.

EVERY TEST HERE TAKES ``pg_schema``, INCLUDING THE ONES THAT WRITE NO ROW. The
autouse isolation points ``SCITEX_STORE_DSN`` at a port nothing listens on, so a
test without the fixture does not read an empty store — it raises on connect.
The tests that begin with no lease are asserting the BOOTSTRAP path, which is a
real read of a real empty store, and they only mean that when the store exists.
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._lifecycle._relocate_effects import RelocateAdapters
from scitex_agent_container._lifecycle._relocate_lease import Lease
from scitex_agent_container._lifecycle._relocate_shell import Shell
from scitex_agent_container._state.relocation_pg import load_lease, save_lease

AGENT = "relocate-handover-test-no-such-session"
SRC = "src-host"
DST = "dst-host"
THIRD = "third-host"
NOW = 1_786_500_000.0
DAY = 86_400.0


def _real_exec(argv, timeout_s=None):
    """Run the rendered script for real. tmux answers for itself."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr,
        "timed_out": False,
    }


def _says_running(argv, timeout_s=None):
    """The one state a test may not create for real: another host running it."""
    return {
        "exit_code": 0,
        "stdout": "TX-RUN=yes\n",
        "stderr": "",
        "timed_out": False,
    }


def _adapters(exec_fn=_real_exec):
    """A handover whose THIRD host is reachable without ssh.

    ``local_host`` is set to the host the lease row names, so the shell built for
    it is LOCAL and its script runs through ``exec_fn`` rather than over the
    network. The observation is still real — it is the same tmux question, asked
    on this machine.
    """
    return RelocateAdapters(
        agent=AGENT,
        spec={},
        from_host=SRC,
        to_host=DST,
        source=Shell(host=SRC, is_local=True),
        target=Shell(host=DST, is_local=True),
        stamp="20260812T000000Z",
        exec_fn=exec_fn,
        now=lambda: NOW,
        local_host=THIRD,
    )


def _row(holder: str, *, fence: int = 1, expires_at: float = NOW + DAY) -> None:
    save_lease(
        Lease(
            agent=AGENT,
            holder=holder,
            token="tok",
            expires_at=expires_at,
            fence=fence,
        )
    )


@pytest.fixture
def stale_row(pg_schema: str) -> None:
    """The canary's return-leg input: a LIVE row naming a host that is not the source.

    Depends on ``pg_schema`` so the store the row is written to is the same
    throwaway schema the phase under test reads back from — and so the tests
    below inherit it without each having to ask.
    """
    _row(THIRD, fence=1)


# ---------------------------------------------------------------------------
# the row that is a record, not a writer
# ---------------------------------------------------------------------------


def test_a_stale_row_no_longer_refuses_the_handover(stale_row) -> None:
    # Arrange: the lease names third-host, which is not running the agent — tmux
    # on this machine answers that for itself.
    adapters = _adapters()
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert result.ok is True


def test_the_lease_actually_moves_to_the_target(stale_row) -> None:
    # Arrange: "it did not refuse" is not "it worked".
    adapters = _adapters()
    # Act
    adapters.hand_over_lease()
    # Assert
    assert load_lease(AGENT).holder == DST


def test_the_fence_advances_twice_past_the_stale_holder(stale_row) -> None:
    # Arrange: once to re-claim for the source, once to hand over. The fence is
    # what locks third-host out if it ever wakes.
    adapters = _adapters()
    # Act
    adapters.hand_over_lease()
    # Assert
    assert load_lease(AGENT).fence == 3


def test_the_journal_records_that_the_holder_was_OBSERVED_absent(stale_row) -> None:
    # Arrange: a reader six months later cannot tell a re-claim on evidence from
    # a re-claim on a clock unless the entry says which it was.
    adapters = _adapters()
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert "OBSERVED not running" in result.detail


def test_the_evidence_reaches_the_adapter_log(stale_row) -> None:
    # Arrange: the observation that permitted this must be auditable.
    adapters = _adapters()
    # Act
    adapters.hand_over_lease()
    # Assert
    assert any("the lease row names third-host" in line for line in adapters.log)


# ---------------------------------------------------------------------------
# the row that IS a live writer
# ---------------------------------------------------------------------------


def test_a_holder_that_is_running_the_agent_still_refuses(stale_row) -> None:
    # Arrange: the split-brain the lease exists to catch. Unchanged behaviour,
    # and the point of the whole change is that THIS still refuses.
    adapters = _adapters(exec_fn=_says_running)
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert result.ok is False


def test_that_refusal_leaves_the_lease_where_it_was(stale_row) -> None:
    # Arrange
    adapters = _adapters(exec_fn=_says_running)
    # Act
    adapters.hand_over_lease()
    # Assert
    assert load_lease(AGENT).holder == THIRD


def test_that_refusal_forbids_forcing_it(stale_row) -> None:
    # Arrange
    adapters = _adapters(exec_fn=_says_running)
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert "do NOT force the handover" in result.hint


# ---------------------------------------------------------------------------
# the ordinary paths, unchanged
# ---------------------------------------------------------------------------


def test_an_empty_store_still_bootstraps_and_hands_over(pg_schema: str) -> None:
    # Arrange: sac claims no lease at agent start, so a first move finds no row.
    adapters = _adapters()
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert result.ok is True


def test_the_bootstrapped_handover_lands_at_fence_one(pg_schema: str) -> None:
    # Arrange
    adapters = _adapters()
    # Act
    adapters.hand_over_lease()
    # Assert
    assert load_lease(AGENT).fence == 1


def test_a_row_already_held_by_the_source_hands_over_directly(pg_schema: str) -> None:
    # Arrange: the ordinary second move.
    _row(SRC, fence=4)
    adapters = _adapters()
    # Act
    adapters.hand_over_lease()
    # Assert
    assert load_lease(AGENT).fence == 5


def test_an_expired_row_is_reclaimed_without_observing_anyone(pg_schema: str) -> None:
    # Arrange: the fence already settles this, so no third host is asked.
    _row(THIRD, fence=1, expires_at=NOW - 1.0)
    adapters = _adapters(exec_fn=_says_running)
    # Act
    result = adapters.hand_over_lease()
    # Assert
    assert result.ok is True
