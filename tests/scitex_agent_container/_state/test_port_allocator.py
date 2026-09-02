"""Tests for the per-agent A2A port allocator.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), asserts exactly one
fact (TQ007), and uses behaviour-revealing docstring-free naming with
``pytest.parametrize`` where the matrix is genuinely declarative
(TQ001). No mocks/monkeypatch — env overrides use explicit save /
restore per PA-306.

VANTAGE CHANGED 2026-08-28, ASSERTIONS DID NOT. ``a2a_ports`` moved to
per-host PostgreSQL, so ``db_path`` is gone from every allocator signature —
it named a file and there is no file. The per-test ``db`` fixture is
replaced by the shared ``pg_schema`` one, which points the REAL resolver at a
throwaway schema; that is a stronger isolation than a temp path was, because
it exercises the resolver production uses instead of bypassing it. Every
Arrange/Act/Assert below is otherwise untouched.

THE COST, STATED: ``pg_schema`` SKIPS where there is no WRITABLE PostgreSQL,
and per the operator's 2026-08-26 ruling every fleet host's loopback is a
read-only replica. So this whole module skips on such a host — including the
two threaded-contention tests that guard the v0.21.19 release failure — and
only executes where a writable database is provisioned. A skip is not a pass.
Point ``SAC_TEST_PG_DSN`` at a throwaway cluster to run them, which also turns
an unusable target from a skip into a hard failure.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from scitex_agent_container._state import port_allocator as pa

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def alpha_claim(pg_schema: str) -> int:
    """Pre-claim a port for ``alpha`` in the small range (20000, 20001)."""
    return pa.claim_port("alpha", range_=(20000, 20001))


# ---------------------------------------------------------------------------
# get_port
# ---------------------------------------------------------------------------


def test_get_port_returns_none_when_agent_never_claimed(pg_schema: str) -> None:
    # Arrange — empty db, no claims.
    name = "nobody"
    # Act
    result = pa.get_port(name)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# claim_port — auto-allocation
# ---------------------------------------------------------------------------


def test_claim_port_second_call_for_same_agent_returns_same_port(
    pg_schema: str,
) -> None:
    # Arrange
    first = pa.claim_port("alpha", range_=(20000, 20100))
    # Act
    second = pa.claim_port("alpha", range_=(20000, 20100))
    # Assert
    assert second == first


def test_claim_port_then_get_port_returns_same_port_for_same_agent(
    pg_schema: str,
) -> None:
    # Arrange
    claimed = pa.claim_port("alpha", range_=(20000, 20100))
    # Act
    looked_up = pa.get_port("alpha")
    # Assert
    assert looked_up == claimed


def test_claim_port_for_second_agent_returns_port_distinct_from_first(
    pg_schema: str, alpha_claim: int
) -> None:
    # Arrange — alpha already holds one slot of the 2-wide range.
    # Act
    beta_port = pa.claim_port("beta", range_=(20000, 20001))
    # Assert
    assert beta_port != alpha_claim


def test_reclaim_after_release_returns_port_distinct_from_other_agent(
    pg_schema: str, alpha_claim: int
) -> None:
    # Arrange — beta takes the other slot, then alpha releases.
    beta_port = pa.claim_port("beta", range_=(20000, 20001))
    pa.release_port("alpha")
    # Act
    re_port = pa.claim_port("alpha", range_=(20000, 20001))
    # Assert
    assert re_port != beta_port


def test_a_released_port_is_reclaimable_by_a_different_agent(
    pg_schema: str,
) -> None:
    """claim -> release -> claim-by-someone-else MUST succeed.

    "A released port must stay re-claimable" is the migration's own title
    requirement, and SOMEONE ELSE is the half a same-agent round trip does
    not cover: the release leaves a tombstone whose ``claimed_by`` still
    names the first holder, so the takeover has to overwrite it. This is
    the test that rules out ``MergeRule.IMMUTABLE`` on ``claimed_by`` —
    under it the takeover put is silently kept-as-first and this goes red
    (measured; see ``port_allocator_store``'s docstring).
    """
    # Arrange — alpha owns the ONLY slot, then releases it as agent_stop does.
    pa.claim_port("alpha", range_=(26000, 26000))
    pa.release_port("alpha")
    # Act — a DIFFERENT agent claims into the same single-wide range.
    port = pa.claim_port("beta", range_=(26000, 26000))
    # Assert
    assert port == 26000


def test_a_port_reclaimed_by_a_different_agent_names_the_new_holder(
    pg_schema: str,
) -> None:
    # Arrange — same round trip as above.
    pa.claim_port("alpha", range_=(26000, 26000))
    pa.release_port("alpha")
    pa.claim_port("beta", range_=(26000, 26000))
    # Act — the ledger as every reader (CLI, listen registry) sees it.
    holders = {c["port"]: c["name"] for c in pa.list_claims()}
    # Assert — the tombstone's old name did not survive the takeover.
    assert holders == {26000: "beta"}


def test_claim_port_raises_runtime_error_when_range_exhausted(
    pg_schema: str,
) -> None:
    # Arrange — single-wide range fully claimed by another agent.
    pa.claim_port("a", range_=(21000, 21000))
    # Act
    second_claim = lambda: pa.claim_port("b", range_=(21000, 21000))
    # Assert
    with pytest.raises(RuntimeError, match="no free a2a port"):
        second_claim()


def test_two_distinct_agents_in_wide_range_get_distinct_ports(
    pg_schema: str,
) -> None:
    # Arrange
    a = pa.claim_port("a", range_=(23000, 23100))
    # Act
    b = pa.claim_port("b", range_=(23000, 23100))
    # Assert
    assert a != b


# ---------------------------------------------------------------------------
# claim_port — explicit pinning
# ---------------------------------------------------------------------------


def test_explicit_claim_records_requested_port_as_returned_value(
    pg_schema: str,
) -> None:
    # Arrange
    explicit = 7901
    # Act
    returned = pa.claim_port("alpha", explicit=explicit)
    # Assert
    assert returned == explicit


def test_explicit_claim_persists_requested_port_for_subsequent_lookup(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=7901)
    # Act
    looked_up = pa.get_port("alpha")
    # Assert
    assert looked_up == 7901


def test_explicit_claim_collides_with_foreign_claim_raises_runtime_error(
    pg_schema: str,
) -> None:
    # Arrange — alpha already pinned 7901.
    pa.claim_port("alpha", explicit=7901)
    # Act
    second_pin = lambda: pa.claim_port("beta", explicit=7901)
    # Assert
    with pytest.raises(RuntimeError, match="already claimed"):
        second_pin()


def test_explicit_repin_for_same_agent_returns_new_port(pg_schema: str) -> None:
    # Arrange — alpha pinned to 7901.
    pa.claim_port("alpha", explicit=7901)
    # Act — operator changes the pin.
    returned = pa.claim_port("alpha", explicit=7902)
    # Assert
    assert returned == 7902


def test_explicit_repin_for_same_agent_persists_new_port_in_lookup(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=7901)
    pa.claim_port("alpha", explicit=7902)
    # Act
    looked_up = pa.get_port("alpha")
    # Assert
    assert looked_up == 7902


# ---------------------------------------------------------------------------
# release_port
# ---------------------------------------------------------------------------


def test_release_port_returns_false_for_agent_that_never_claimed(
    pg_schema: str,
) -> None:
    # Arrange — empty db.
    # Act
    released = pa.release_port("nobody")
    # Assert
    assert released is False


def test_release_port_returns_true_after_successful_prior_claim(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("alpha", range_=(22000, 22100))
    # Act
    released = pa.release_port("alpha")
    # Assert
    assert released is True


def test_release_port_clears_claim_so_get_port_returns_none(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("alpha", range_=(22000, 22100))
    pa.release_port("alpha")
    # Act
    looked_up = pa.get_port("alpha")
    # Assert
    assert looked_up is None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_threaded_contention_assigns_distinct_ports_to_every_winner(
    pg_schema: str,
) -> None:
    # Arrange — 8 threads racing on an 8-wide range; UNIQUE(port)
    # serialises the inserts so each thread must land on its own slot.
    n = 8
    rng = (24000, 24000 + n - 1)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            p = pa.claim_port(f"agent-{i}", range_=rng)
            with lock:
                results.append(p)
        except BaseException as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Assert — single fact: every result is unique.
    assert len(set(results)) == n


def test_threaded_contention_records_no_exceptions(pg_schema: str) -> None:
    # Arrange
    n = 8
    rng = (24100, 24100 + n - 1)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            pa.claim_port(f"agent-{i}", range_=rng)
        except BaseException as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# list_claims
# ---------------------------------------------------------------------------


def test_list_claims_returns_row_count_matching_number_of_claims(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("a", range_=(25000, 25100))
    pa.claim_port("b", range_=(25000, 25100))
    # Act
    rows = pa.list_claims()
    # Assert
    assert len(rows) == 2


def test_list_claims_returns_rows_whose_names_match_claimed_agents(
    pg_schema: str,
) -> None:
    # Arrange
    pa.claim_port("a", range_=(25000, 25100))
    pa.claim_port("b", range_=(25000, 25100))
    # Act
    names = {r["name"] for r in pa.list_claims()}
    # Assert
    assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# the per-process Store cache
# ---------------------------------------------------------------------------


def test_port_store_returns_the_same_cached_handle_within_a_process(
    pg_schema: str,
) -> None:
    # Arrange — first call populates the per-process cache (card
    # store-connect-cost-per-call-20260828: Store.__init__ pays a
    # psycopg connect, so the agent-start path must not construct per call).
    first = pa.port_store()
    # Act
    second = pa.port_store()
    # Assert — IDENTITY, not equality: the connect was paid once.
    assert second is first


def test_reset_store_cache_hands_out_a_fresh_handle(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.port_allocator_store import (
        _reset_store_cache,
    )

    first = pa.port_store()
    # Act — the plain reset tests use instead of monkeypatching the cache.
    _reset_store_cache()
    # Assert
    assert pa.port_store() is not first


# ---------------------------------------------------------------------------
# config.yaml port_range override
# ---------------------------------------------------------------------------


@pytest.fixture
def config_env(tmp_path: Path):
    """Redirect ``_default_config_path`` at a tmp ``config.yaml``.

    Explicit save/restore of ``$SCITEX_AGENT_CONTAINER_CONFIG`` per
    PA-306 (no monkeypatch). Yields the path so each test writes the
    YAML it wants exercised.
    """
    cfg = tmp_path / "config.yaml"
    key = "SCITEX_AGENT_CONTAINER_CONFIG"
    saved = os.environ.get(key)
    os.environ[key] = str(cfg)
    try:
        yield cfg
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_config_yaml_port_range_constrains_auto_claim_within_bounds(
    pg_schema: str, config_env: Path
) -> None:
    # Arrange — config narrows the range to a 2-wide window.
    config_env.write_text("a2a:\n  port_range: [30000, 30001]\n")
    # Act
    p = pa.claim_port("alpha")
    # Assert — single fact: the claimed port lies inside the
    # configured window (membership is the behaviour under test).
    assert p in (30000, 30001)
