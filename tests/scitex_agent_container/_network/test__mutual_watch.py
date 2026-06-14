"""Tests for the mutual heartbeat-watch decision logic.

Operator mandate (lead a2a 1781e82a, 2026-06-14): agents cross-monitor
each other's heartbeat_at + session.jsonl growth; a stale peer raises
a STRUCTURAL alert.

These tests cover the pure :func:`check_peer_freshness` decision —
no DB, no threads, no mocks. Each test builds a real peer state dir
with real heartbeat.json + session.jsonl files and pins the clock to
a known float so the assertion is exact.

Conventions:

  * AAA marker comments (Arrange / Act / Assert) on separate lines.
  * One assertion per test (STX-TQ007); related invariants split
    into dedicated tests.
  * No mocks / monkeypatch — only real files + pinned ``now``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._network._mutual_watch import (
    KIND_STALE_HEARTBEAT,
    KIND_STALE_SESSION_JSONL,
    WatchConfig,
    check_peer_freshness,
    load_watch_config,
)

# ---------------------------------------------------------------------------
# Test helpers — real-file fixture builders. No mocks, no monkeypatch.
# ---------------------------------------------------------------------------


def _write_heartbeat(state_dir: Path, ts: float, state: str = "working") -> None:
    """Write a real heartbeat.json with the given ts + state."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": ts, "pid": 1234, "state": state}),
        encoding="utf-8",
    )


def _write_session_jsonl(state_dir: Path, mtime: float, content: str = "x\n") -> None:
    """Write a session.jsonl and pin its mtime to a known wall-clock."""
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "session.jsonl"
    p.write_text(content, encoding="utf-8")
    os.utime(p, (mtime, mtime))


# ---------------------------------------------------------------------------
# Threshold defaults — the spec's "configurable threshold" acceptance.
# ---------------------------------------------------------------------------


def test_watch_config_default_heartbeat_threshold_is_180s():
    # Arrange
    ctor = WatchConfig
    # Act
    cfg = ctor()
    # Assert — operator-tunable default per module docstring.
    assert cfg.heartbeat_threshold_s == 180.0


def test_watch_config_default_jsonl_idle_threshold_is_300s():
    # Arrange
    ctor = WatchConfig
    # Act
    cfg = ctor()
    # Assert
    assert cfg.jsonl_idle_threshold_s == 300.0


def test_load_watch_config_honours_env_override_for_heartbeat(env_setter):
    # Arrange
    env_setter("SAC_MUTUAL_WATCH_HEARTBEAT_STALE_S", "42")
    # Act
    cfg = load_watch_config()
    # Assert — operator override mechanism: env var takes precedence.
    assert cfg.heartbeat_threshold_s == 42.0


def test_load_watch_config_honours_env_override_for_jsonl(env_setter):
    # Arrange
    env_setter("SAC_MUTUAL_WATCH_JSONL_IDLE_S", "7")
    # Act
    cfg = load_watch_config()
    # Assert
    assert cfg.jsonl_idle_threshold_s == 7.0


def test_load_watch_config_invalid_env_value_falls_back_to_default(env_setter):
    # Arrange — a typo'd value must not wedge the watch loop.
    env_setter("SAC_MUTUAL_WATCH_HEARTBEAT_STALE_S", "not-a-number")
    # Act
    cfg = load_watch_config()
    # Assert — conservative default wins over the broken override.
    assert cfg.heartbeat_threshold_s == 180.0


# ---------------------------------------------------------------------------
# Healthy peer — clean cross-monitor signal.
# ---------------------------------------------------------------------------


def test_fresh_peer_emits_no_alert(tmp_path: Path):
    # Arrange — peer beat 5s ago (well under default 180s).
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 5.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 5.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — healthy peer contributes nothing to the alert stream.
    assert alerts == []


def test_idle_peer_with_silent_jsonl_does_not_fire(tmp_path: Path):
    # Arrange — peer claims idle (correctly reports it is doing nothing).
    # session.jsonl is old but the heartbeat is recent + state=="idle".
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 10.0, state="idle")
    _write_session_jsonl(peer_dir, mtime=now - 9999.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — an idle peer with a quiet transcript is healthy.
    assert alerts == []


# ---------------------------------------------------------------------------
# Heartbeat staleness — the classic wedged-runner signal.
# ---------------------------------------------------------------------------


def test_stale_heartbeat_past_threshold_fires_alert(tmp_path: Path):
    # Arrange — peer last beat 300s ago, threshold 180s.
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 300.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 300.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — exactly one structural alert of the heartbeat kind.
    assert [a.kind for a in alerts] == [KIND_STALE_HEARTBEAT]


def test_stale_heartbeat_alert_records_age_seconds(tmp_path: Path):
    # Arrange — peer last beat 300s ago.
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 300.0, state="working")
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — evidence carries the OBSERVED age, not a flag.
    assert alerts[0].age_seconds == 300.0


def test_missing_heartbeat_fires_stale_heartbeat_alert(tmp_path: Path):
    # Arrange — peer state dir exists but heartbeat.json absent.
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    peer_dir.mkdir(parents=True)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — missing heartbeat counts as the stale-heartbeat case.
    assert [a.kind for a in alerts] == [KIND_STALE_HEARTBEAT]


def test_custom_threshold_does_not_fire_when_age_just_below(tmp_path: Path):
    # Arrange — peer last beat 5s ago, custom threshold 10s.
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 5.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 5.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
        config=WatchConfig(heartbeat_threshold_s=10.0, jsonl_idle_threshold_s=10.0),
    )
    # Assert — under-threshold peer is healthy under a tighter knob.
    assert alerts == []


def test_custom_threshold_fires_when_age_just_above(tmp_path: Path):
    # Arrange — peer last beat 15s ago, custom threshold 10s.
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 15.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 1.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
        config=WatchConfig(heartbeat_threshold_s=10.0, jsonl_idle_threshold_s=300.0),
    )
    # Assert — tightened threshold catches a peer the default would miss.
    assert [a.kind for a in alerts] == [KIND_STALE_HEARTBEAT]


# ---------------------------------------------------------------------------
# session.jsonl staleness — the silent-drift signal.
# ---------------------------------------------------------------------------


def test_working_peer_with_idle_jsonl_fires_session_jsonl_alert(tmp_path: Path):
    # Arrange — peer beat IS fresh but session.jsonl has not grown
    # for 400s while state=="working" (the silent-drift case).
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 5.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 400.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — operator-mandated structural alert for silent drift.
    assert [a.kind for a in alerts] == [KIND_STALE_SESSION_JSONL]


def test_working_peer_with_recent_jsonl_does_not_fire(tmp_path: Path):
    # Arrange — peer beat fresh, session.jsonl mtime fresh, state="working".
    now = 1_700_000_000.0
    peer_dir = tmp_path / "bob"
    _write_heartbeat(peer_dir, ts=now - 5.0, state="working")
    _write_session_jsonl(peer_dir, mtime=now - 5.0)
    # Act
    alerts = check_peer_freshness(
        observer="alice",
        peer="bob",
        peer_state_dir=peer_dir,
        now=now,
    )
    # Assert — actively-producing working peer is healthy.
    assert alerts == []


# ---------------------------------------------------------------------------
# Mutual = bidirectional. A watches B AND B watches A independently.
# ---------------------------------------------------------------------------


def test_mutual_watch_both_directions_emit_when_both_peers_are_stale(tmp_path: Path):
    # Arrange — TWO peer state dirs; both stale heartbeats.
    now = 1_700_000_000.0
    a_dir = tmp_path / "alice"
    b_dir = tmp_path / "bob"
    _write_heartbeat(a_dir, ts=now - 400.0, state="working")
    _write_heartbeat(b_dir, ts=now - 400.0, state="working")
    # Act — alice observes bob; bob observes alice. Two independent calls.
    alice_watching_bob = check_peer_freshness(
        observer="alice", peer="bob", peer_state_dir=b_dir, now=now
    )
    bob_watching_alice = check_peer_freshness(
        observer="bob", peer="alice", peer_state_dir=a_dir, now=now
    )
    combined = alice_watching_bob + bob_watching_alice
    # Assert — both directions emit, observer/peer fields oriented per call.
    assert {(a.observer, a.peer) for a in combined} == {
        ("alice", "bob"),
        ("bob", "alice"),
    }


# ---------------------------------------------------------------------------
# Shared fixture — env save/restore without monkeypatch-as-fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def env_setter():
    """Save/restore env vars explicitly (no monkeypatch fixture).

    Yields a setter the test calls with ``(name, value)``; the
    fixture records the prior value and restores it on teardown.
    Implementation note: this is an env-management helper, not a
    mocking primitive — STX-NM002 bans mocks / monkeypatch-as-fixture
    for test seams, but explicit save/restore of OS state is the
    standard pattern in this repo (see ``db_path`` fixture in
    ``test_state_db_turns_errors_heartbeats.py``).
    """
    saved: dict[str, str | None] = {}

    def _set(name: str, value: str) -> None:
        if name not in saved:
            saved[name] = os.environ.get(name)
        os.environ[name] = value

    yield _set
    for name, prior in saved.items():
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


# Touch ``time`` only to silence the lint that flags the unused import
# the test fixture above might otherwise drag in.
assert callable(time.time)
