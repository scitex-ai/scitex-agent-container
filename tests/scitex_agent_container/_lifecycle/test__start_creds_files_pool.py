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
from scitex_agent_container._lifecycle._quota_evidence import UNVERIFIABLE_MARKER
from scitex_agent_container._lifecycle._start import _rotate_to_healthy_account
from scitex_agent_container.config import AgentConfig


def _without_quota_warning(log: str) -> str:
    """Drop the unverifiable-quota warning, keeping every other line.

    The autouse fixture below points the reader at an ABSENT cache, so the
    preflight now warns once per boot that it could not confirm the picked
    account's headroom (see ``._quota_evidence``). That is an orthogonal
    signal; the assertions here are about POOL-SELECTION output. The marker is
    imported rather than spelled out so a reworded warning cannot silently
    start slipping past this filter.
    """
    kept = [line for line in log.splitlines() if UNVERIFIABLE_MARKER not in line]
    return "".join(f"{line}\n" for line in kept)


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


@pytest.fixture(autouse=True)
def _isolate_quota_cache(tmp_path: Path) -> Iterator[None]:
    """Point the quota-cache reader at a nonexistent tmp file.

    Agent containers bind the LIVE fleet ``/var/sac/quota-cache.json`` (the
    reader's default). Without this these boot tests would read real fleet
    utilisation AND, since the fail-loud gate keys off cache PRESENCE, the live
    bind would make an un-injected pick trip the gate. An absent override keeps
    them hermetic (no cache present → freshness-only degrade) — quota reaches
    the picker only through the ``usage_*`` injection seams.
    """
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(tmp_path / "absent-quota-cache.json")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


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


def test_pool_rotates_off_near_capped_entry_to_a_healthy_sibling(
    _isolate_home: Path,
) -> None:
    # Arrange — 3 fresh accounts; the first is near-capped so the pick
    # must land on one of the two healthy siblings (WHICH one is the
    # per-agent load-balancing hash's choice, so assert the set).
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
    # Assert — the near-capped acct-a is avoided.
    assert cfg.claude.credentials_file in {str(p_b), str(p_c)}


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


def test_pool_notice_names_the_active_policy(_isolate_home: Path) -> None:
    # Arrange — the notice must say WHICH 7d spend policy ranked the pick.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(
        cfg, log_stream=log, usage_7d={"acct-a": 95.0, "acct-b": 5.0}
    )
    # Assert
    assert "policy=spread" in log.getvalue()


def test_pool_notice_carries_per_candidate_ranking_inputs(
    _isolate_home: Path,
) -> None:
    # Arrange — operator 2026-07-17: the notice named its CRITERIA but
    # not its INPUTS, so a reasoned pick was indistinguishable from a
    # lucky one. Every candidate's 5h/7d/reset must appear.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(
        cfg, log_stream=log, usage_7d={"acct-a": 95.0, "acct-b": 5.0}
    )
    # Assert — the losing candidate's inputs are logged too.
    assert "ranking inputs:" in log.getvalue() and "acct-a(5h=" in log.getvalue()


@pytest.fixture
def _burn_policy_env() -> Iterator[None]:
    """Set the REAL SAC_CREDS_7D_POLICY=burn env var; restore on teardown."""
    saved = os.environ.get("SAC_CREDS_7D_POLICY")
    saved_long = os.environ.pop("SCITEX_AGENT_CONTAINER_CREDS_7D_POLICY", None)
    os.environ["SAC_CREDS_7D_POLICY"] = "burn"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_CREDS_7D_POLICY", None)
        else:
            os.environ["SAC_CREDS_7D_POLICY"] = saved
        if saved_long is not None:
            os.environ["SCITEX_AGENT_CONTAINER_CREDS_7D_POLICY"] = saved_long


def test_pool_under_burn_env_prefers_the_near_capped_entry(
    _isolate_home: Path, _burn_policy_env: None
) -> None:
    # Arrange — SAC_CREDS_7D_POLICY=burn (fixture). Under the corrected
    # 7d rule the near-capped entry is a reason to PICK: its unspent
    # quota is destroyed at the weekly boundary.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    # Act
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_7d={"acct-a": 95.0, "acct-b": 5.0},
    )
    # Assert — spread avoids acct-a; burn drains it to zero.
    assert cfg.claude.credentials_file == str(p_a)


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
    # Assert — a 1-element pool that keeps its entry narrates no selection.
    assert _without_quota_warning(log.getvalue()) == ""


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


# ---------------------------------------------------------------------------
# 5h axis — a pool entry at its 5h wall cannot run NOW and must be avoided
# (2026-07 `sac-restart --all-running` incident: 100%-5h account picked
# because its 7d % looked fine).
# ---------------------------------------------------------------------------


def test_pool_avoids_entry_at_its_5h_cap_despite_7d_headroom(
    _isolate_home: Path,
) -> None:
    # Arrange — the first entry is fresh with 7d headroom but at 100% of
    # its 5h window; the two others are idle.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    p_c = _write_snapshot(home, "acct-c", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b, p_c])
    # Act
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_5h={"acct-a": 100.0, "acct-b": 0.0, "acct-c": 0.0},
        usage_7d={"acct-a": 60.0, "acct-b": 25.0, "acct-c": 2.0},
    )
    # Assert — the blocked-now entry is never bound while others are idle.
    assert cfg.claude.credentials_file in {str(p_b), str(p_c)}


def test_pool_still_boots_when_every_entry_is_5h_blocked(
    _isolate_home: Path,
) -> None:
    # Arrange — the whole pool is at its 5h wall. Quota is a preference:
    # the boot must still bind SOME fresh entry rather than fail.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    # Act
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_5h={"acct-a": 100.0, "acct-b": 98.0},
        usage_7d={"acct-a": 60.0, "acct-b": 25.0},
    )
    # Assert
    assert cfg.claude.credentials_file in {str(p_a), str(p_b)}


# ---------------------------------------------------------------------------
# Fleet load-balancing — a bulk restart must SPREAD agents across the
# healthy entries instead of stacking every agent onto the same one.
# ---------------------------------------------------------------------------


def test_pool_spreads_a_fleet_of_agents_across_healthy_entries(
    _isolate_home: Path,
) -> None:
    # Arrange — two healthy entries with comparable headroom and a
    # 12-agent fleet listing the SAME pool (the incident shape).
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    # Act
    picks = set()
    for i in range(12):
        cfg = _make_pool_config(f"agent-{i}", [p_a, p_b])
        _rotate_to_healthy_account(
            cfg,
            log_stream=io.StringIO(),
            usage_5h={"acct-a": 0.0, "acct-b": 0.0},
            usage_7d={"acct-a": 25.0, "acct-b": 2.0},
        )
        picks.add(cfg.claude.credentials_file)
    # Assert — both accounts serve part of the fleet.
    assert picks == {str(p_a), str(p_b)}


def test_pool_keeps_currently_effective_entry_when_it_is_healthy(
    _isolate_home: Path,
) -> None:
    # Arrange — the agent's credentials_file already points at a listed,
    # healthy, un-capped entry: churn minimisation must keep it even if
    # a sibling has more headroom.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    cfg = _make_pool_config("alpha", [p_a, p_b])
    cfg.claude.credentials_file = str(p_a)
    # Act
    _rotate_to_healthy_account(
        cfg,
        log_stream=io.StringIO(),
        usage_5h={"acct-a": 10.0, "acct-b": 0.0},
        usage_7d={"acct-a": 40.0, "acct-b": 2.0},
    )
    # Assert — no rotation off a healthy current entry.
    assert cfg.claude.credentials_file == str(p_a)


def test_pool_first_listed_entry_is_not_implicitly_preferred(
    _isolate_home: Path,
) -> None:
    # Arrange — no pin, no current file. Pre-fix, slugs[0] was treated as
    # "preferred" and kept whenever below the 7d near-cap — which stacked
    # every fleet agent onto the first listed account. With both entries
    # equally idle, a 12-agent fleet must now spread instead.
    home = _isolate_home
    p_a = _write_snapshot(home, "acct-a", _future_ms())
    p_b = _write_snapshot(home, "acct-b", _future_ms())
    # Act
    picks = set()
    for i in range(12):
        cfg = _make_pool_config(f"agent-{i}", [p_a, p_b])
        _rotate_to_healthy_account(
            cfg,
            log_stream=io.StringIO(),
            usage_5h={"acct-a": 0.0, "acct-b": 0.0},
            usage_7d={"acct-a": 30.0, "acct-b": 30.0},
        )
        picks.add(cfg.claude.credentials_file)
    # Assert
    assert picks == {str(p_a), str(p_b)}
