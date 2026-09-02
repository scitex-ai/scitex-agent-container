"""claim_port under CONCURRENCY — the bug that ghosted the v0.21.19 release.

THE INCIDENT. The release gate (self-hosted Spartan, inside the SIF, pytest
under `-n $(nproc)` xdist) died on:

    IntegrityError: UNIQUE constraint failed: a2a_ports.port

…while the PR gate stayed GREEN. It could not have been otherwise: the PR gate
ran `pytest tests/` SINGLE-PROCESS on a hosted ubuntu box, so it was
STRUCTURALLY INCAPABLE of observing a race. The tag was pushed, `test` failed,
`build`/`publish`/`release` were skipped, and PyPI got nothing — a ghost tag.
v0.21.18 died the same way with a different symptom (`no free a2a port`), which
is the signature of a race: the failure MOVES.

THE MECHANISM. ``claim_port``'s ``explicit`` branch was a TOCTOU:

    clash = SELECT name FROM a2a_ports WHERE port=?   # (1) check
    if clash: raise RuntimeError(...)
    INSERT INTO a2a_ports ...                         # (2) use

Two statements. A concurrent claimant that lands between (1) and (2) makes the
INSERT trip ``UNIQUE(port)``, and the raw driver exception escaped to the
caller. Whether you got the INTENDED ``RuntimeError`` or a raw
``IntegrityError`` was decided purely by thread timing.

WHY A PLAIN `--force` RESTART REACHES THE PIN BRANCH AT ALL (this is the part
that makes it bite in the wild, not just for operator-pinned specs):
``resolve_a2a_port`` MUTATES ``config.a2a.port`` in place, "auto" -> the int it
just claimed. ``agent_start``'s force/restart path then re-resolves AFTER
``agent_stop`` has released the row — and that second call sees an *int*, so it
takes the ``explicit`` branch. An auto-port agent therefore traverses the
pinned-port code on every forced restart.

NOT A TEST-ONLY BUG. On a real host two concurrent restarts race here and the
operator gets a raw driver traceback instead of a diagnosis.

No mocks: a real database, the real ``claim_port``, real threads. The
``Barrier`` is what makes it deterministic rather than a 1-in-N flake — it
holds every thread until all of them are ready, so they all cross the TOCTOU
window together. Pre-fix this reproduced ~6 raw IntegrityError escapes out of
16 threads on the first run.

THE BACKEND MOVED 2026-08-28, and this module had to move with it in a way
that is more than plumbing. ``a2a_ports`` now lives in per-host PostgreSQL, so
the per-test file path is replaced by the shared ``pg_schema`` fixture. Two
assertions needed REWORDING rather than re-pointing, and both are the same
mistake in different clothes — an assertion that names the OLD backend keeps
passing while measuring nothing:

  * "no raw driver ``IntegrityError`` escaped" is trivially true once the
    driver is psycopg. It is now stated as "everything that escaped is the
    diagnostic ``RuntimeError``", which is backend-neutral and stronger.
  * a direct ``SELECT ... FROM a2a_ports`` cannot run at all. It is now a
    ``list_claims()`` read, which is the public surface and is where the
    uniqueness lives: ``port`` is the store's IDENTITY field, so the store
    carries the invariant structurally rather than through a UNIQUE index.

THE COST, STATED: ``pg_schema`` SKIPS where there is no WRITABLE PostgreSQL,
so the release-gate regression these tests guard is not exercised on a host
whose loopback is a read-only replica. A skip is not a pass. Point
``SAC_TEST_PG_DSN`` at a throwaway cluster to run them.
"""

from __future__ import annotations

import threading

import pytest

from scitex_agent_container._state.port_allocator import claim_port, list_claims


def _claim_concurrently(
    n_threads: int,
    target,
) -> list[BaseException]:
    """Run ``target(i)`` on ``n_threads`` threads released simultaneously.

    Returns every exception that escaped, so the caller can assert on the
    TYPES that reached the caller rather than merely on "it didn't crash".
    """
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()  # all threads cross the TOCTOU window together
        try:
            target(i)
        except BaseException as exc:  # noqa: BLE001 — cataloguing what escapes
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    return errors


def test_concurrent_explicit_pin_never_leaks_a_raw_driver_error(
    pg_schema: str,
) -> None:
    # Arrange — 16 DISTINCT agents all pinned to the SAME port. Exactly one can
    # legitimately win; the other 15 must lose CLEANLY.

    # Act
    errors = _claim_concurrently(
        16,
        lambda i: claim_port(f"agent-{i}", explicit=19000),
    )

    # Assert — the regression: a raw driver exception must NEVER reach the
    # caller. Stated as "everything that escapes is the diagnostic
    # RuntimeError" rather than naming a driver ``IntegrityError``, because the
    # backend moved to PostgreSQL and a check spelled for the OLD driver would
    # have gone permanently, invisibly vacuous — psycopg's UniqueViolation is
    # not a driver IntegrityError, so the original assertion would pass while
    # sixteen raw psycopg tracebacks reached the operator. Backend-neutral is
    # also strictly stronger: it catches the next driver too.
    leaked = [e for e in errors if not isinstance(e, RuntimeError)]
    assert not leaked, (
        f"{len(leaked)}/16 threads leaked a raw driver exception "
        f"(the shape of the v0.21.19 release failure): {leaked[:3]}"
    )


def test_auto_origin_reclaim_of_a_stolen_port_gets_a_fresh_port_not_an_error(
    pg_schema: str,
) -> None:
    # Arrange — the RESTART case, and the one that matters for a fleet
    # relaunch. `resolve_a2a_port` mutates "auto" -> an int, so a restarting
    # agent re-claims its old port as an `explicit` value. Here that port was
    # taken while it was down. It is NOT an operator pin, so the agent must
    # come back on a NEW port. A "clean RuntimeError" here is still a DEAD
    # AGENT, and the operator relaunches ~14 at once.
    claim_port("thief", explicit=19000)

    # Act
    port = claim_port("restarter", explicit=19000, explicit_is_pin=False)

    # Assert
    assert port != 19000


def test_operator_pin_is_not_silently_downgraded_to_a_different_port(
    pg_schema: str,
) -> None:
    # Arrange — the guard on the fix above. A GENUINE operator pin is a
    # contract: if it is held by someone else that is a real misconfiguration
    # and must stay loud. The auto-origin fallback must not have quietly
    # swallowed it.
    claim_port("incumbent", explicit=19000)
    caught: BaseException | None = None

    # Act
    try:
        claim_port("pinned", explicit=19000, explicit_is_pin=True)
    except BaseException as exc:  # noqa: BLE001 — asserting on the TYPE below
        caught = exc

    # Assert
    assert isinstance(caught, RuntimeError)


def test_concurrent_explicit_pin_losers_all_get_the_diagnostic_runtimeerror(
    pg_schema: str,
) -> None:
    # Arrange

    # Act
    errors = _claim_concurrently(
        16,
        lambda i: claim_port(f"agent-{i}", explicit=19000),
    )

    # Assert — 16 claimants, 1 winner => exactly 15 losers, and every one of
    # them gets the SAME actionable error naming the port. Deterministic
    # outcome, not a timing lottery.
    assert len(errors) == 15 and all(isinstance(e, RuntimeError) for e in errors)


def test_concurrent_explicit_pin_commits_exactly_one_winning_row(
    pg_schema: str,
) -> None:
    # Arrange

    # Act
    _claim_concurrently(
        16,
        lambda i: claim_port(f"agent-{i}", explicit=19000),
    )

    # Assert — the ledger is not merely un-crashed, it is CORRECT: one claim
    # holds the contended port. Read through ``list_claims`` rather than by
    # opening the backend directly: the store IS the identity uniqueness now
    # (``port`` is the store's IDENTITY field), so there is no separate
    # UNIQUE constraint to inspect, and a test that reached past the public
    # surface would be asserting on a schema instead of on behaviour.
    holders = [c for c in list_claims() if c["port"] == 19000]
    assert len(holders) == 1


def test_same_agent_racing_itself_keeps_its_pin_idempotently(
    pg_schema: str,
) -> None:
    # Arrange — the SAME agent started twice concurrently (the force-restart
    # re-claim). claim_port is documented idempotent on agent_name, and a race
    # must not turn a legitimate re-entry into a failure.

    # Act
    errors = _claim_concurrently(
        8,
        lambda _i: claim_port("solo", explicit=19000),
    )

    # Assert
    assert not errors, f"idempotent re-claim raised: {errors[:3]}"


def test_concurrent_auto_claims_all_get_distinct_ports(pg_schema: str) -> None:
    # Arrange — the auto branch already handled contention (ascending scan +
    # optimistic insert, IntegrityError -> re-scan). Pin that contract so a
    # future "simplification" of the loop cannot silently regress it.
    claimed: list[int] = []
    lock = threading.Lock()

    def claim(i: int) -> None:
        port = claim_port(f"auto-{i}", range_=(19000, 19999))
        with lock:
            claimed.append(port)

    # Act
    errors = _claim_concurrently(16, claim)

    # Assert — no escapes, and every agent got its OWN port.
    assert not errors and len(set(claimed)) == 16


@pytest.mark.parametrize("pinned", [19000, 19500])
def test_explicit_pin_still_reports_a_genuine_foreign_claim(
    pg_schema: str, pinned: int
) -> None:
    # Arrange — the UNCONTENDED path must keep its original behaviour: a real
    # misconfiguration (two specs pinning one port) is still a loud error. The
    # race fix must not have swallowed the diagnosis it exists to preserve.
    claim_port("incumbent", explicit=pinned)
    caught: BaseException | None = None

    # Act
    try:
        claim_port("newcomer", explicit=pinned)
    except BaseException as exc:  # noqa: BLE001 — asserting on the TYPE below
        caught = exc

    # Assert
    assert isinstance(
        caught, RuntimeError
    ) and f"a2a port {pinned} already claimed" in str(caught)
