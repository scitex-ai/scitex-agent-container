"""Tests for the ``credentials_files`` (plural) quota-aware account pool.

This is the wiring that makes the quota-aware pick (PR #583/#584) affect
``credentials_file``-pinned fleet agents: a spec lists MULTIPLE account
credential files and the start pre-flight
(:func:`_lifecycle._start_preflight._rotate_to_healthy_account`) picks ONE
of them QUOTA-AWARE, collapsing the pool down to
``config.claude.credentials_file`` (the field every downstream auth path
already binds).

PA-306: no mocks. Real config dataclasses, real store snapshots, real
``_rotate_to_healthy_account`` mutation against an isolated ``$HOME``.
Quota + freshness are injected via the picker's documented override params
(``usage_7d`` / ``now``) — no real quota cache, no network. AAA markers,
descriptive names, one assertion each.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds import NoHealthyAccountError
from scitex_agent_container._lifecycle._start import _rotate_to_healthy_account
from scitex_agent_container.config import AgentConfig


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _snapshot_path(home: Path, slug: str) -> Path:
    return (
        home / ".scitex" / "agent-container" / "accounts" / slug / ".credentials.json"
    )


def _write_snapshot(home: Path, slug: str, expires_at_ms: int) -> Path:
    path = _snapshot_path(home, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}}))
    return path


def _future_ms(seconds: float = 7200.0) -> int:
    return int((time.time() + seconds) * 1_000)


def _past_ms(seconds: float = 600.0) -> int:
    return int((time.time() - seconds) * 1_000)


def _make_pool_config(name: str, paths: list[Path]) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.credentials_files = [str(p) for p in paths]
    return cfg


# ---------------------------------------------------------------------------
# Pool of 3 fresh entries — quota-aware pick selects the most-headroom one.
# ---------------------------------------------------------------------------


def test_pool_picks_most_headroom_fresh_entry(_isolate_home: Path) -> None:
    # Arrange — 3 fresh accounts; the FIRST (preferred) is near-capped so
    # the pick must rotate off it to the lowest-7d fresh sibling.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    p_c = _write_snapshot(home, "acct-c", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b, p_c])
    # Act — inject per-account 7d utilisation (no real cache).
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_7d={"acct-a": 95.0, "acct-b": 10.0, "acct-c": 40.0},
    )
    # Assert — acct-b has the most 7d headroom → its file is bound.
    assert cfg.claude.credentials_file == str(p_b)


def test_pool_selection_emits_a_one_line_notice(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(
        cfg, log_stream=log, usage_7d={"acct-a": 95.0, "acct-b": 5.0}
    )
    # Assert — operator sees WHICH agent and WHICH account was selected.
    msg = log.getvalue()
    assert "alpha" in msg and "acct-b" in msg


# ---------------------------------------------------------------------------
# Only one entry is token-fresh — it is returned even when near-capped.
# ---------------------------------------------------------------------------


def test_pool_returns_capped_but_only_fresh_entry(_isolate_home: Path) -> None:
    # Arrange — a + c EXPIRED, only b is fresh (and near its weekly cap).
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _past_ms(60))
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    p_c = _write_snapshot(home, "acct-c", _past_ms(60))
    cfg = _make_pool_config("alpha", [p_a, p_b, p_c])
    # Act — headroom is a preference, not a hard gate.
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_7d={"acct-a": 5.0, "acct-b": 96.0, "acct-c": 5.0},
    )
    # Assert — the only fresh account wins despite being near-capped.
    assert cfg.claude.credentials_file == str(p_b)


# ---------------------------------------------------------------------------
# Nothing fresh in the pool — fail loud.
# ---------------------------------------------------------------------------


def test_pool_all_expired_raises_no_healthy_account_error(_isolate_home: Path) -> None:
    # Arrange — every listed snapshot is expired.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _past_ms(60))
    p_b = _write_snapshot(home, "acct-b", _past_ms(60))
    cfg = _make_pool_config("alpha", [p_a, p_b])
    # Act
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert — no silent stale-token launch.
    with ctx:
        _rotate_to_healthy_account(cfg)


# ---------------------------------------------------------------------------
# Back-compat: singular credentials_file still resolves to its one account.
# ---------------------------------------------------------------------------


def test_singular_credentials_file_resolves_to_that_one_account(
    _isolate_home: Path,
) -> None:
    # Arrange — legacy singular field, one fresh snapshot, no pool.
    home = _isolate_home
    p = _write_snapshot(home, "acct-solo", _future_ms())
    cfg = AgentConfig(name="alpha")
    cfg.claude.credentials_file = str(p)
    # Act — treated as a 1-element pool; pick returns it.
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())
    # Assert — the designated file is unchanged (pure no-op).
    assert cfg.claude.credentials_file == str(p)


def test_singular_credentials_file_emits_no_notice(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    p = _write_snapshot(home, "acct-solo", _future_ms())
    cfg = AgentConfig(name="alpha")
    cfg.claude.credentials_file = str(p)
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert — a 1-element pool that keeps its entry logs nothing.
    assert log.getvalue() == ""


# ---------------------------------------------------------------------------
# Empty / absent — unchanged legacy behaviour (no pool, no pin).
# ---------------------------------------------------------------------------


def test_no_pool_no_pin_leaves_credentials_file_empty(_isolate_home: Path) -> None:
    # Arrange — unpinned agent: no credentials_files, no credentials_file.
    cfg = AgentConfig(name="alpha")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert — host live OAuth path untouched.
    assert cfg.claude.credentials_file == "" and cfg.claude.account == ""
