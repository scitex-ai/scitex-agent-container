"""A host with NO quota cache must BUILD the evidence, never go silent.

The blind-pick gate (:func:`_creds.pick_healthy_account` ``require_quota_evidence``)
exists for the case "the cache tells us nothing", yet the start preflight armed it
only when a cache FILE already existed. On a host with NO cache — exactly the blind
case — the gate was DISARMED and the boot proceeded blind, AND the armed path's
auto-refresh self-repair never ran either. The host that most needs the cache built
was the one host that never tried to build it.

MEASURED 2026-08-06, scitex-02 (a newly-provisioned compute node with no quota cron
and no ``quota-cache.json``): the pick read "5h=? 7d=?", landed agent ``figrecipe``
on ``wyusuuke-gmail-com`` at d7=100.0%, sac printed ``SUCC``, tmux was alive, the
TUI rendered — and every turn answered "You've hit your weekly limit". Startup
reported success; the agent was functionally dead.

What is locked here
-------------------
1. No cache + a refresh that SUCCEEDS → evidence is built, the gate ends up armed,
   and the exhausted account is NOT picked while a healthy sibling exists.
2. No cache + a refresh that genuinely CANNOT run → the boot still proceeds (the
   never-block invariant: a boot is never blocked merely because this host runs no
   quota system) AND a loud warning NAMES the picked account.
3. That degraded boot leaves no durable brick: a SECOND boot on the same host still
   proceeds. A failed refresh writes ``{"accounts": {}}``, and the mere EXISTENCE of
   that file arms ``require_quota_evidence`` on every later boot — which would
   convert the never-block degrade into a permanent hard refusal.

PA-306: no mocks. Real snapshots, real store, real ``refresh_quota_cache`` through
its ordinary code path, real ``_rotate_to_healthy_account`` mutation. The refresh is
kept offline WITHOUT patching it: :func:`_account.claude_usage.fetch_usage_for_credentials`
consults its production per-account cache (``<account_dir>/usage.json``, 5-min TTL)
BEFORE any token read or network call, so seeding that real file is enough for the
success case, and a snapshot with no ``accessToken`` is enough for the failure case.
AAA markers (TQ002); descriptive names; one assertion each (TQ007).
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds import BlindQuotaCacheError
from scitex_agent_container._lifecycle._start import _rotate_to_healthy_account
from scitex_agent_container.config import AgentConfig

# The scitex-02 incident pair. Distinct first dash-segments, because that
# segment is the quota cache's per-account match key (``short``).
_EXHAUSTED = "wyusuuke-gmail-com"  # d7 = 100.0% — the account that was picked
_HEALTHY = "ywatanabe-scitex-ai"  # d7 = 5.0% — the sibling that should have won

# One agent name per fleet member. With every account's quota UNKNOWN the pick
# falls to per-agent rendezvous hashing, which spreads the fleet across BOTH
# accounts; with evidence the near-capped account is a whole tier worse and no
# agent lands on it. A fleet therefore separates "picked blind" from "picked
# with evidence" deterministically, where a single agent could agree by luck.
_FLEET = tuple(f"figrecipe-{i}" for i in range(12))

# The degraded-boot warning's stable marker. Asserted as a SPACED phrase on
# purpose: pytest derives ``tmp_path`` from the test's own name, and the
# selection notice prints that path — so a single-word marker echoed by the
# path would make the assertion pass against a build that warns about
# nothing. A space cannot occur in these paths. (Measured on the unfixed
# tree: a `"unverifiable" in log` assertion passed for exactly that reason.)
_MARKER = "QUOTA UNVERIFIABLE"


def _warning_line(log: str) -> str:
    """The degraded-boot warning only — never the ordinary selection notice.

    The notice already names the agent and the picked account, so an
    unscoped substring search cannot tell "warned about this account" from
    "mentioned it while reporting a normal pick".
    """
    return "\n".join(line for line in log.splitlines() if _MARKER in line)


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


@pytest.fixture
def _no_quota_cache(tmp_path: Path) -> Iterator[Path]:
    """Point the cache READER and the populator at one path holding NO file.

    ``SAC_QUOTA_CACHE_PATH`` is the shared override for both ends, so this is
    the honest shape of a quota-cron-less host: nothing to read, and a refresh
    would write exactly where the reader looks.
    """
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    cache = tmp_path / "runtime" / "quota-cache.json"
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(cache)
    try:
        yield cache
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _write_snapshot(store: Path, slug: str, *, with_token: bool) -> Path:
    """Write one account's ``.credentials.json`` and return its path.

    ``with_token`` decides whether the usage fetch can even begin: without an
    ``accessToken`` :func:`fetch_usage_for_credentials` returns an error dict
    before touching the network, which is the offline stand-in for "no
    credentials on this host".
    """
    path = store / slug / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, object] = {"expiresAt": int((time.time() + 7_200.0) * 1_000)}
    if with_token:
        oauth["accessToken"] = "not-a-real-token"
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return path


def _seed_usage_cache(creds: Path, *, pct_5h: float, pct_7d: float) -> None:
    """Seed the REAL per-account usage cache the populator's fetcher reads.

    ``<account_dir>/usage.json`` is production state, not a test double: it is
    the file :func:`_account.claude_usage.fetch_usage_for_credentials` writes
    and consults first (5-min TTL), and the same file
    ``_state.account_store.read_account_usage_cache`` reads. Seeding it lets
    ``refresh_quota_cache`` succeed through its ordinary path with no network.
    """
    (creds.parent / "usage.json").write_text(
        json.dumps(
            {
                "used_pct_5h": pct_5h,
                "used_pct_7d": pct_7d,
                "reset_at_5h": None,
                "reset_at_7d": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
        )
    )


def _default_store(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _pool_config(name: str, paths: list[Path]) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.credentials_files = [str(p) for p in paths]
    return cfg


# ---------------------------------------------------------------------------
# 1. No cache + a refresh that SUCCEEDS — build the evidence, then use it.
# ---------------------------------------------------------------------------


def test_no_cache_host_builds_evidence_and_never_picks_the_exhausted_account(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange — the scitex-02 shape: two token-fresh accounts, one at its
    # weekly cap, and NO quota cache anywhere the reader looks. Both accounts
    # are measurable offline (seeded per-account usage cache), so the
    # on-demand refresh can succeed.
    store = _default_store(_isolate_home)
    p_hot = _write_snapshot(store, _EXHAUSTED, with_token=True)
    p_cool = _write_snapshot(store, _HEALTHY, with_token=True)
    _seed_usage_cache(p_hot, pct_5h=12.0, pct_7d=100.0)
    _seed_usage_cache(p_cool, pct_5h=3.0, pct_7d=5.0)

    # Act — boot a fleet that all lists the same pool.
    picks = set()
    for agent in _FLEET:
        cfg = _pool_config(agent, [p_hot, p_cool])
        _rotate_to_healthy_account(cfg, log_stream=io.StringIO())
        picks.add(cfg.claude.credentials_file)

    # Assert — nobody lands on the d7=100% account; the cache-less host
    # measured the pool instead of guessing.
    assert picks == {str(p_cool)}


def test_no_cache_host_leaves_a_populated_quota_cache_behind(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange
    store = _default_store(_isolate_home)
    p_hot = _write_snapshot(store, _EXHAUSTED, with_token=True)
    p_cool = _write_snapshot(store, _HEALTHY, with_token=True)
    _seed_usage_cache(p_hot, pct_5h=12.0, pct_7d=100.0)
    _seed_usage_cache(p_cool, pct_5h=3.0, pct_7d=5.0)
    cfg = _pool_config("figrecipe", [p_hot, p_cool])

    # Act
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())

    # Assert — the evidence is durable, so the NEXT boot (and the operator)
    # can see the same utilisation without re-measuring.
    assert json.loads(_no_quota_cache.read_text())["accounts"].keys() == {
        _EXHAUSTED,
        _HEALTHY,
    }


# ---------------------------------------------------------------------------
# 2. No cache + a refresh that genuinely CANNOT run — degrade, but LOUDLY.
# ---------------------------------------------------------------------------


def test_boot_proceeds_when_no_cache_exists_and_the_refresh_cannot_run(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange — token-fresh snapshots with no usable credential for the usage
    # API (the CI / fresh-install / quota-cron-less shape). The refresh runs
    # and measures nothing.
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])

    # Act — a boot is never blocked merely because this host runs no quota
    # system.
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())

    # Assert
    assert cfg.claude.credentials_file in {str(p_a), str(p_b)}


def test_degraded_boot_warning_line_names_the_account_it_could_not_verify(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange — same unmeasurable host as above.
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])
    log = io.StringIO()

    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)

    # Assert — the slug must appear IN THE WARNING, not merely somewhere in
    # the output: the ordinary selection notice already names it, so an
    # unscoped search would pass against a build that warns about nothing.
    picked_slug = Path(cfg.claude.credentials_file).parent.name
    assert picked_slug in _warning_line(log.getvalue())


def test_degraded_boot_warning_states_the_condition(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])
    log = io.StringIO()

    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)

    # Assert — the warning states the CONDITION, not just the pick.
    assert _MARKER in log.getvalue()


def test_degraded_boot_warning_gives_the_command_that_populates_the_cache(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])
    log = io.StringIO()

    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)

    # Assert — actionable, not merely alarming.
    assert "sac accounts refresh-quota-cache" in log.getvalue()


# ---------------------------------------------------------------------------
# 3. The degraded boot must not brick the NEXT one.
# ---------------------------------------------------------------------------


def test_failed_refresh_does_not_arm_the_gate_for_the_next_boot(
    _isolate_home: Path, _no_quota_cache: Path
) -> None:
    # Arrange — a failed refresh writes ``{"accounts": {}}``, and the mere
    # EXISTENCE of that file is what arms ``require_quota_evidence``. If it is
    # left behind, every later boot on this host hard-refuses instead of
    # degrading — a permanent brick built by the self-repair itself.
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    _rotate_to_healthy_account(
        _pool_config("figrecipe", [p_a, p_b]), log_stream=io.StringIO()
    )
    second = _pool_config("figrecipe", [p_a, p_b])

    # Act — the SECOND boot on the same host.
    _rotate_to_healthy_account(second, log_stream=io.StringIO())

    # Assert — still never blocked.
    assert second.claude.credentials_file in {str(p_a), str(p_b)}


# ---------------------------------------------------------------------------
# 4. Present cache — the armed path is UNCHANGED (auto-refresh once, then the
#    refusal stands). These lock behaviour the fix must not disturb.
# ---------------------------------------------------------------------------


@pytest.fixture
def _blind_quota_cache(tmp_path: Path) -> Iterator[Path]:
    """A cache FILE that exists and holds NOTHING — the armed-but-blind host."""
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    cache = tmp_path / "runtime" / "quota-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"written_at": 1_784_530_000.0, "accounts": {}}))
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(cache)
    try:
        yield cache
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def test_present_but_blind_cache_is_auto_refreshed_and_then_ranked(
    _isolate_home: Path, _blind_quota_cache: Path
) -> None:
    # Arrange — the operator's 2026-08-02 ask (「refresh quota cache 勝手にやれよ」):
    # a present-but-empty cache must be repaired by sac, not by hand. Both
    # accounts are measurable offline, so the one auto-refresh succeeds.
    store = _default_store(_isolate_home)
    p_hot = _write_snapshot(store, _EXHAUSTED, with_token=True)
    p_cool = _write_snapshot(store, _HEALTHY, with_token=True)
    _seed_usage_cache(p_hot, pct_5h=12.0, pct_7d=100.0)
    _seed_usage_cache(p_cool, pct_5h=3.0, pct_7d=5.0)
    cfg = _pool_config("figrecipe", [p_hot, p_cool])

    # Act
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())

    # Assert — repaired, then ranked on the repaired evidence.
    assert cfg.claude.credentials_file == str(p_cool)


def test_present_cache_still_blind_after_the_refresh_refuses_the_boot(
    _isolate_home: Path, _blind_quota_cache: Path
) -> None:
    # Arrange — a cache exists but nothing can be measured, so the refresh
    # cannot clear the blindness. Refusing to boot on unverifiable quota
    # (constitution §2: unknown is not "OK") must still stand.
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])

    # Act
    ctx = pytest.raises(BlindQuotaCacheError)

    # Assert
    with ctx:
        _rotate_to_healthy_account(cfg, log_stream=io.StringIO())


def test_populated_cache_boot_emits_no_unverifiable_warning(
    _isolate_home: Path, _blind_quota_cache: Path
) -> None:
    # Arrange — evidence is available, so the degraded path must not fire. A
    # warning that also appears on healthy boots is one nobody reads.
    store = _default_store(_isolate_home)
    p_hot = _write_snapshot(store, _EXHAUSTED, with_token=True)
    p_cool = _write_snapshot(store, _HEALTHY, with_token=True)
    _seed_usage_cache(p_hot, pct_5h=12.0, pct_7d=100.0)
    _seed_usage_cache(p_cool, pct_5h=3.0, pct_7d=5.0)
    cfg = _pool_config("figrecipe", [p_hot, p_cool])
    log = io.StringIO()

    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)

    # Assert
    assert _MARKER not in log.getvalue()
