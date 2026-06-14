"""Tests for the mutual-watch sweep orchestrator + alert persistence.

Covers the side-effect layer that sits between the pure decision
(``_mutual_watch``) and the operator surface (``structural_alerts``
table). The sweep walks peers, persists alerts via
:mod:`state_db_alerts`, and resolves prior-active alerts when the
peer recovers.

Conventions:

  * AAA marker comments (Arrange / Act / Assert) on separate lines.
  * One assertion per test (STX-TQ007).
  * No mocks / monkeypatch — real state.db (tmp path via env), real
    files for peer state dirs.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures — isolated state.db + isolated state_root.
# ---------------------------------------------------------------------------


@pytest.fixture
def state_env(tmp_path: Path):
    """Pin SCITEX_AGENT_CONTAINER_STATE_DB + RUNTIME_DIR to a tmp path.

    Yields a dict with ``db_path`` and ``state_root`` so the test can
    write peer state dirs and read back ``structural_alerts`` rows.
    Explicit env save/restore (no monkeypatch fixture) per repo
    convention.
    """
    saved = {
        "SCITEX_AGENT_CONTAINER_STATE_DB": os.environ.get(
            "SCITEX_AGENT_CONTAINER_STATE_DB"
        ),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
        ),
    }
    db_path = tmp_path / "state.db"
    state_root = tmp_path / "runtime"
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db_path)
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(state_root)
    # Reload modules that snapshot env at import.
    import scitex_agent_container._state.state_db as _db

    importlib.reload(_db)
    import scitex_agent_container._runners._session_state as _ss

    importlib.reload(_ss)
    try:
        yield {"db_path": db_path, "state_root": state_root}
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        importlib.reload(_db)
        importlib.reload(_ss)


def _write_heartbeat(state_dir: Path, ts: float, state: str = "working") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": ts, "pid": 4242, "state": state}),
        encoding="utf-8",
    )


def _write_session_jsonl(state_dir: Path, mtime: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "session.jsonl"
    p.write_text("x\n", encoding="utf-8")
    os.utime(p, (mtime, mtime))


# ---------------------------------------------------------------------------
# Schema + persistence.
# ---------------------------------------------------------------------------


def test_init_schema_creates_structural_alerts_table(state_env):
    # Arrange
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    import sqlite3

    with sqlite3.connect(state_env["db_path"]) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Assert
    assert "structural_alerts" in names


def test_record_alert_inserts_row_with_evidence(state_env):
    # Arrange
    from scitex_agent_container._state.state_db_alerts import (
        KIND_STALE_HEARTBEAT,
        list_active_alerts,
        record_alert,
    )

    # Act
    record_alert(
        observer="alice",
        peer="bob",
        kind=KIND_STALE_HEARTBEAT,
        evidence={"age_seconds": 300.0},
        now=1_700_000_000.0,
    )
    rows = list_active_alerts()
    # Assert — exactly one active row landed.
    assert len(rows) == 1


def test_record_alert_dedupes_repeat_fires_into_one_row(state_env):
    # Arrange — three sweeps observe the same staleness.
    from scitex_agent_container._state.state_db_alerts import (
        KIND_STALE_HEARTBEAT,
        list_active_alerts,
        record_alert,
    )

    # Act
    for ts in (1_700_000_000.0, 1_700_000_060.0, 1_700_000_120.0):
        record_alert(observer="alice", peer="bob", kind=KIND_STALE_HEARTBEAT, now=ts)
    rows = list_active_alerts()
    # Assert — dedup means ONE row, hit_count bumped each time.
    assert rows[0]["hit_count"] == 3


def test_resolve_alert_marks_row_resolved(state_env):
    # Arrange
    from scitex_agent_container._state.state_db_alerts import (
        KIND_STALE_HEARTBEAT,
        list_active_alerts,
        record_alert,
        resolve_alert,
    )

    record_alert(observer="alice", peer="bob", kind=KIND_STALE_HEARTBEAT, now=1.0)
    # Act
    resolve_alert(observer="alice", peer="bob", kind=KIND_STALE_HEARTBEAT, now=2.0)
    # Assert — active list is empty after resolve.
    assert list_active_alerts() == []


# ---------------------------------------------------------------------------
# Sweep — orchestration over real peer state dirs + DB writes.
# ---------------------------------------------------------------------------


def test_sweep_emits_alert_when_peer_heartbeat_is_stale(state_env):
    # Arrange — bob's state dir under the real state root.
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers

    now = 1_700_000_000.0
    bob_dir = state_env["state_root"] / "bob"
    _write_heartbeat(bob_dir, ts=now - 400.0, state="working")
    _write_session_jsonl(bob_dir, mtime=now - 5.0)
    # Act
    emitted = sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=now,
    )
    # Assert — the stale heartbeat is surfaced as a structural alert.
    assert [a.kind for a in emitted] == ["stale_heartbeat"]


def test_sweep_persists_alert_into_structural_alerts_table(state_env):
    # Arrange
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers
    from scitex_agent_container._state.state_db_alerts import list_active_alerts

    now = 1_700_000_000.0
    bob_dir = state_env["state_root"] / "bob"
    _write_heartbeat(bob_dir, ts=now - 400.0, state="working")
    # Act
    sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=now,
    )
    rows = list_active_alerts(observer="alice", peer="bob")
    # Assert — DB has the row the operator + lead read from.
    assert rows[0]["kind"] == "stale_heartbeat"


def test_sweep_resolves_prior_alert_when_peer_recovers(state_env):
    # Arrange — first sweep observes stale heartbeat, second sweep sees recovery.
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers
    from scitex_agent_container._state.state_db_alerts import list_active_alerts

    bob_dir = state_env["state_root"] / "bob"
    sweep_now = 1_700_000_000.0
    _write_heartbeat(bob_dir, ts=sweep_now - 400.0, state="working")
    sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=sweep_now,
    )
    # Peer recovers: fresh heartbeat.
    recover_now = sweep_now + 10.0
    _write_heartbeat(bob_dir, ts=recover_now - 5.0, state="working")
    _write_session_jsonl(bob_dir, mtime=recover_now - 5.0)
    # Act
    sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=recover_now,
    )
    # Assert — the recovery sweep cleared the alert.
    assert list_active_alerts(observer="alice", peer="bob") == []


def test_sweep_does_not_fire_for_healthy_peer(state_env):
    # Arrange
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers

    now = 1_700_000_000.0
    bob_dir = state_env["state_root"] / "bob"
    _write_heartbeat(bob_dir, ts=now - 5.0, state="working")
    _write_session_jsonl(bob_dir, mtime=now - 5.0)
    # Act
    emitted = sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=now,
    )
    # Assert — clean cross-monitor signal.
    assert emitted == []


def test_sweep_is_mutual_each_observer_sees_its_peers_independently(state_env):
    # Arrange — alice + bob both stale, alice watches bob and bob watches alice.
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers
    from scitex_agent_container._state.state_db_alerts import list_active_alerts

    now = 1_700_000_000.0
    alice_dir = state_env["state_root"] / "alice"
    bob_dir = state_env["state_root"] / "bob"
    _write_heartbeat(alice_dir, ts=now - 400.0, state="working")
    _write_heartbeat(bob_dir, ts=now - 400.0, state="working")
    # Act — two observers run independent sweeps.
    sweep_peers(
        observer="alice",
        peer_names=["bob"],
        state_root=state_env["state_root"],
        now=now,
    )
    sweep_peers(
        observer="bob",
        peer_names=["alice"],
        state_root=state_env["state_root"],
        now=now,
    )
    rows = list_active_alerts()
    # Assert — both directions persisted.
    assert {(r["observer"], r["peer"]) for r in rows} == {
        ("alice", "bob"),
        ("bob", "alice"),
    }


def test_sweep_skips_observer_watching_itself(state_env):
    # Arrange — observer name appears in peer_names (e.g. registry self-row).
    from scitex_agent_container._network._mutual_watch_sweep import sweep_peers

    now = 1_700_000_000.0
    alice_dir = state_env["state_root"] / "alice"
    _write_heartbeat(alice_dir, ts=now - 400.0, state="working")
    # Act
    emitted = sweep_peers(
        observer="alice",
        peer_names=["alice"],
        state_root=state_env["state_root"],
        now=now,
    )
    # Assert — self-watch is not the mutual-monitoring signal the spec asks for.
    assert emitted == []


# Quietly use ``time`` so import-linting is happy if a future test
# needs wall-clock fixtures.
assert callable(time.time)
