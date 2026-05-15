"""Tests for the per-agent A2A port allocator.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), asserts exactly one
fact (TQ007), and uses behaviour-revealing docstring-free naming with
``pytest.parametrize`` where the matrix is genuinely declarative
(TQ001). No mocks/monkeypatch — env overrides use explicit save /
restore per PA-306.
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
def db(tmp_path: Path) -> Path:
    """A per-test state.db path; allocator creates the schema on demand."""
    return tmp_path / "state.db"


@pytest.fixture
def alpha_claim(db: Path) -> int:
    """Pre-claim a port for ``alpha`` in the small range (20000, 20001)."""
    return pa.claim_port("alpha", range_=(20000, 20001), db_path=db)


# ---------------------------------------------------------------------------
# get_port
# ---------------------------------------------------------------------------


def test_get_port_returns_none_when_agent_never_claimed(db: Path) -> None:
    # Arrange — empty db, no claims.
    name = "nobody"
    # Act
    result = pa.get_port(name, db_path=db)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# claim_port — auto-allocation
# ---------------------------------------------------------------------------


def test_claim_port_second_call_for_same_agent_returns_same_port(
    db: Path,
) -> None:
    # Arrange
    first = pa.claim_port("alpha", range_=(20000, 20100), db_path=db)
    # Act
    second = pa.claim_port("alpha", range_=(20000, 20100), db_path=db)
    # Assert
    assert second == first


def test_claim_port_then_get_port_returns_same_port_for_same_agent(
    db: Path,
) -> None:
    # Arrange
    claimed = pa.claim_port("alpha", range_=(20000, 20100), db_path=db)
    # Act
    looked_up = pa.get_port("alpha", db_path=db)
    # Assert
    assert looked_up == claimed


def test_claim_port_for_second_agent_returns_port_distinct_from_first(
    db: Path, alpha_claim: int
) -> None:
    # Arrange — alpha already holds one slot of the 2-wide range.
    # Act
    beta_port = pa.claim_port("beta", range_=(20000, 20001), db_path=db)
    # Assert
    assert beta_port != alpha_claim


def test_reclaim_after_release_returns_port_distinct_from_other_agent(
    db: Path, alpha_claim: int
) -> None:
    # Arrange — beta takes the other slot, then alpha releases.
    beta_port = pa.claim_port("beta", range_=(20000, 20001), db_path=db)
    pa.release_port("alpha", db_path=db)
    # Act
    re_port = pa.claim_port("alpha", range_=(20000, 20001), db_path=db)
    # Assert
    assert re_port != beta_port


def test_claim_port_raises_runtime_error_when_range_exhausted(
    db: Path,
) -> None:
    # Arrange — single-wide range fully claimed by another agent.
    pa.claim_port("a", range_=(21000, 21000), db_path=db)
    # Act
    second_claim = lambda: pa.claim_port("b", range_=(21000, 21000), db_path=db)
    # Assert
    with pytest.raises(RuntimeError, match="no free a2a port"):
        second_claim()


def test_two_distinct_agents_in_wide_range_get_distinct_ports(
    db: Path,
) -> None:
    # Arrange
    a = pa.claim_port("a", range_=(23000, 23100), db_path=db)
    # Act
    b = pa.claim_port("b", range_=(23000, 23100), db_path=db)
    # Assert
    assert a != b


# ---------------------------------------------------------------------------
# claim_port — explicit pinning
# ---------------------------------------------------------------------------


def test_explicit_claim_records_requested_port_as_returned_value(
    db: Path,
) -> None:
    # Arrange
    explicit = 7901
    # Act
    returned = pa.claim_port("alpha", explicit=explicit, db_path=db)
    # Assert
    assert returned == explicit


def test_explicit_claim_persists_requested_port_for_subsequent_lookup(
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=7901, db_path=db)
    # Act
    looked_up = pa.get_port("alpha", db_path=db)
    # Assert
    assert looked_up == 7901


def test_explicit_claim_collides_with_foreign_claim_raises_runtime_error(
    db: Path,
) -> None:
    # Arrange — alpha already pinned 7901.
    pa.claim_port("alpha", explicit=7901, db_path=db)
    # Act
    second_pin = lambda: pa.claim_port("beta", explicit=7901, db_path=db)
    # Assert
    with pytest.raises(RuntimeError, match="already claimed"):
        second_pin()


def test_explicit_repin_for_same_agent_returns_new_port(db: Path) -> None:
    # Arrange — alpha pinned to 7901.
    pa.claim_port("alpha", explicit=7901, db_path=db)
    # Act — operator changes the pin.
    returned = pa.claim_port("alpha", explicit=7902, db_path=db)
    # Assert
    assert returned == 7902


def test_explicit_repin_for_same_agent_persists_new_port_in_lookup(
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=7901, db_path=db)
    pa.claim_port("alpha", explicit=7902, db_path=db)
    # Act
    looked_up = pa.get_port("alpha", db_path=db)
    # Assert
    assert looked_up == 7902


# ---------------------------------------------------------------------------
# release_port
# ---------------------------------------------------------------------------


def test_release_port_returns_false_for_agent_that_never_claimed(
    db: Path,
) -> None:
    # Arrange — empty db.
    # Act
    released = pa.release_port("nobody", db_path=db)
    # Assert
    assert released is False


def test_release_port_returns_true_after_successful_prior_claim(
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("alpha", range_=(22000, 22100), db_path=db)
    # Act
    released = pa.release_port("alpha", db_path=db)
    # Assert
    assert released is True


def test_release_port_clears_claim_so_get_port_returns_none(
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("alpha", range_=(22000, 22100), db_path=db)
    pa.release_port("alpha", db_path=db)
    # Act
    looked_up = pa.get_port("alpha", db_path=db)
    # Assert
    assert looked_up is None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_threaded_contention_assigns_distinct_ports_to_every_winner(
    db: Path,
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
            p = pa.claim_port(f"agent-{i}", range_=rng, db_path=db)
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


def test_threaded_contention_records_no_exceptions(db: Path) -> None:
    # Arrange
    n = 8
    rng = (24100, 24100 + n - 1)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            pa.claim_port(f"agent-{i}", range_=rng, db_path=db)
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
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("a", range_=(25000, 25100), db_path=db)
    pa.claim_port("b", range_=(25000, 25100), db_path=db)
    # Act
    rows = pa.list_claims(db_path=db)
    # Assert
    assert len(rows) == 2


def test_list_claims_returns_rows_whose_names_match_claimed_agents(
    db: Path,
) -> None:
    # Arrange
    pa.claim_port("a", range_=(25000, 25100), db_path=db)
    pa.claim_port("b", range_=(25000, 25100), db_path=db)
    # Act
    names = {r["name"] for r in pa.list_claims(db_path=db)}
    # Assert
    assert names == {"a", "b"}


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
    tmp_path: Path, config_env: Path
) -> None:
    # Arrange — config narrows the range to a 2-wide window.
    config_env.write_text("a2a:\n  port_range: [30000, 30001]\n")
    db_path = tmp_path / "state.db"
    # Act
    p = pa.claim_port("alpha", db_path=db_path)
    # Assert — single fact: the claimed port lies inside the
    # configured window (membership is the behaviour under test).
    assert p in (30000, 30001)
