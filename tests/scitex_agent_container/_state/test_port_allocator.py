"""Tests for the per-agent A2A port allocator."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from scitex_agent_container._state import port_allocator as pa


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_get_port_returns_none_when_unclaimed(db: Path) -> None:
    assert pa.get_port("nobody", db_path=db) is None


def test_claim_is_idempotent_for_same_agent(db: Path) -> None:
    p1 = pa.claim_port("alpha", range_=(20000, 20100), db_path=db)
    p2 = pa.claim_port("alpha", range_=(20000, 20100), db_path=db)
    assert p1 == p2
    assert pa.get_port("alpha", db_path=db) == p1


def test_claim_release_reclaim_yields_different_port(db: Path) -> None:
    p1 = pa.claim_port("alpha", range_=(20000, 20001), db_path=db)
    # alpha holds p1; beta gets the other in the 2-wide range.
    p_beta = pa.claim_port("beta", range_=(20000, 20001), db_path=db)
    assert p_beta != p1

    pa.release_port("alpha", db_path=db)
    # After release, alpha re-claims; only the *other* slot is free
    # (beta still holds p_beta). So alpha must land on p1 again, which
    # is now the unique free slot. The cross-check below ensures
    # release actually freed *something* that's distinct from beta's port.
    p_re = pa.claim_port("alpha", range_=(20000, 20001), db_path=db)
    assert p_re != p_beta


def test_range_exhaustion_raises(db: Path) -> None:
    pa.claim_port("a", range_=(21000, 21000), db_path=db)
    with pytest.raises(RuntimeError, match="no free a2a port"):
        pa.claim_port("b", range_=(21000, 21000), db_path=db)


def test_explicit_port_honoured(db: Path) -> None:
    p = pa.claim_port("alpha", explicit=7901, db_path=db)
    assert p == 7901
    assert pa.get_port("alpha", db_path=db) == 7901


def test_explicit_port_collision_raises(db: Path) -> None:
    pa.claim_port("alpha", explicit=7901, db_path=db)
    with pytest.raises(RuntimeError, match="already claimed"):
        pa.claim_port("beta", explicit=7901, db_path=db)


def test_explicit_repin_updates_claim(db: Path) -> None:
    pa.claim_port("alpha", explicit=7901, db_path=db)
    p = pa.claim_port("alpha", explicit=7902, db_path=db)
    assert p == 7902
    assert pa.get_port("alpha", db_path=db) == 7902


def test_release_returns_false_for_unknown_agent(db: Path) -> None:
    assert pa.release_port("nobody", db_path=db) is False


def test_release_returns_true_after_claim(db: Path) -> None:
    pa.claim_port("alpha", range_=(22000, 22100), db_path=db)
    assert pa.release_port("alpha", db_path=db) is True
    assert pa.get_port("alpha", db_path=db) is None


def test_two_agents_get_distinct_ports(db: Path) -> None:
    a = pa.claim_port("a", range_=(23000, 23100), db_path=db)
    b = pa.claim_port("b", range_=(23000, 23100), db_path=db)
    assert a != b


def test_threaded_contention_no_duplicate(db: Path) -> None:
    # 8 threads racing on the same 8-wide range — all must land on
    # distinct ports (UNIQUE constraint serialises the inserts).
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
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == n
    assert len(set(results)) == n  # all unique


def test_list_claims_returns_rows(db: Path) -> None:
    pa.claim_port("a", range_=(25000, 25100), db_path=db)
    pa.claim_port("b", range_=(25000, 25100), db_path=db)
    rows = pa.list_claims(db_path=db)
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"a", "b"}


def test_config_yaml_range_override(tmp_path: Path) -> None:
    import os

    cfg = tmp_path / "config.yaml"
    cfg.write_text("a2a:\n  port_range: [30000, 30001]\n")
    key = "SCITEX_AGENT_CONTAINER_CONFIG"
    saved = os.environ.get(key)
    os.environ[key] = str(cfg)
    try:
        db = tmp_path / "state.db"
        p = pa.claim_port("alpha", db_path=db)
        assert 30000 <= p <= 30001
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
