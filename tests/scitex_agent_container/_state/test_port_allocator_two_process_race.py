"""Two REAL processes racing one port — the exclusion the store cannot give.

WHY PROCESSES AND NOT THREADS. ``test_port_allocator_concurrency.py`` races
threads, and threads are the one population the store's own guard DOES cover:
``Store.put`` runs its read-check-write under a ``threading.RLock``, so an
in-process race is serialised before the protocol in
``port_allocator_store.try_claim`` is ever exercised. The claim ledger's real
racers are separate PROCESSES (two ``sac agents start`` on one host — the
operator relaunches ~14 at once), where that lock does not exist and the
rows write is a per-field UPSERT that never trips the primary key. Only a
multi-process race exercises the settled claim protocol's actual guarantees:
``NEW_RECORD`` catches the sequential loser, and the MANDATORY read-back is
how the concurrent loser LEARNS it lost (PR #1243, comment 5451759350).

WHAT IS PROVEN vs ARGUED. Proven here: two real processes, one real
PostgreSQL store, a fork barrier releasing both claims together — exactly one
winner, and the loser's diagnostic names the winner it read back. Argued, not
proven: the sub-millisecond window where BOTH read-backs could interleave
between the two materialisations (each seeing its own write) is not
deterministically reachable from a test — if it ever fires, THIS test is the
one that goes red, which is the correct alarm.

VANTAGE. ``pg_schema`` points ``SCITEX_STORE_DSN`` at a throwaway schema and
the children INHERIT that environment through fork, so both processes
converge on the same store the fixture will drop. The fork start method is
requested explicitly: it is what a real ``sac`` relaunch looks like
process-wise, and it also exercises the pid half of the module's Store cache
key (a child must build its own connection, never adopt the parent's fd).

No mocks, no monkeypatch (PA-306): real allocator, real store, real fork.
"""

from __future__ import annotations

import multiprocessing

from scitex_agent_container._state import port_allocator as pa

#: The contended port. Outside DEFAULT_RANGE so a stray claim cannot collide.
RACED_PORT = 27000

#: How long a child waits at the barrier / the parent waits on a child.
_TIMEOUT_S = 30


def _race_claim(agent_name: str, barrier, results) -> None:
    """One racer: cross the barrier, claim the pin, report what happened.

    Runs in the CHILD. Reports a tuple ``(agent, outcome, detail)`` where
    outcome is ``won`` (detail = the port), ``lost`` (detail = the
    diagnostic message, which names the winner claim_port read back) or
    ``error`` (anything else — never expected, always reported rather than
    swallowed so the parent's assertion sees it).
    """
    try:
        barrier.wait(timeout=_TIMEOUT_S)
        port = pa.claim_port(agent_name, explicit=RACED_PORT, explicit_is_pin=True)
        results.put((agent_name, "won", str(port)))
    except RuntimeError as exc:
        results.put((agent_name, "lost", str(exc)))
    except BaseException as exc:  # noqa: BLE001 — cataloguing what escapes
        results.put((agent_name, "error", f"{type(exc).__name__}: {exc}"))


def _run_race() -> list[tuple[str, str, str]]:
    """Fork two claimants, release them together, collect both outcomes.

    ARRANGE helper: precondition failures raise rather than assert, so they
    are never mistaken for the fact a test measures.
    """
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    racers = [
        ctx.Process(target=_race_claim, args=(f"racer-{i}", barrier, results))
        for i in range(2)
    ]
    for proc in racers:
        proc.start()
    outcomes = [results.get(timeout=_TIMEOUT_S) for _ in racers]
    for proc in racers:
        proc.join(timeout=_TIMEOUT_S)
        if proc.is_alive():  # pragma: no cover - hung child
            proc.terminate()
            raise RuntimeError("arrange failed: a racer did not exit")
    errored = [o for o in outcomes if o[1] == "error"]
    if errored:
        raise RuntimeError(f"arrange failed: racer escaped with {errored}")
    return outcomes


def test_two_processes_racing_one_pin_produce_exactly_one_winner(
    pg_schema: str,
) -> None:
    # Arrange — the fixture's throwaway schema is the whole arrangement.
    # Act — the race itself.
    outcomes = _run_race()
    # Assert — EXACTLY one winner: not zero (a claim must land) and not two
    # (the silent double-claim the read-back protocol exists to prevent).
    assert sum(1 for o in outcomes if o[1] == "won") == 1


def test_the_losing_process_is_told_who_won_by_the_read_back(
    pg_schema: str,
) -> None:
    # Arrange — the fixture's throwaway schema is the whole arrangement.
    # Act
    outcomes = _run_race()
    winners = [o for o in outcomes if o[1] == "won"]
    losers = [o for o in outcomes if o[1] == "lost"]
    if len(winners) != 1 or len(losers) != 1:
        raise RuntimeError(
            f"arrange failed: expected 1 winner + 1 loser, got {outcomes}"
        )
    # Assert — the loser LEARNED it lost from the shared store: its
    # diagnostic names the winning agent, which claim_port can only know by
    # reading the record back after its own put did not stick.
    assert winners[0][0] in losers[0][2]


def test_two_process_race_commits_exactly_one_ledger_row(pg_schema: str) -> None:
    # Arrange — the fixture's throwaway schema is the whole arrangement.
    # Act
    _run_race()
    # Assert — the ledger converged on ONE live claim for the raced port.
    holders = [c for c in pa.list_claims() if c["port"] == RACED_PORT]
    assert len(holders) == 1
