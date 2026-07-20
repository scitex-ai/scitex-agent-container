"""Boot fail-loud gate keys off quota-cache PRESENCE (``quota_cache_present``).

The start preflight (:func:`_lifecycle._start._rotate_to_healthy_account`) sets
``require_quota_evidence`` only when a quota-cache FILE exists on the host. This
locks the discriminator end-to-end (constitution §2 — unknown is not "OK"):

* a fleet host WITH a cache whose populator produced nothing (empty/stale) →
  fail loud rather than boot an unverifiable account (2026-07-20 incident:
  scitex-cards launched on a 7d=100% account read as "5h=? 7d=?");
* a cache-LESS host (fresh install / CI / quota-cron-less Spartan node) →
  degrade to freshness-only and boot (the documented never-block invariant).

PA-306: no mocks — real snapshots, real cache files, real ``_rotate`` mutation.
AAA markers (TQ002); descriptive names; one assertion each (TQ007).
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


@pytest.fixture
def _restore_quota_cache_env() -> Iterator[None]:
    """Restore ``SAC_QUOTA_CACHE_PATH`` after a test sets it in Arrange.

    Each test points the reader at a DIFFERENT path (a present cache file vs an
    absent one), so the value is set per-test rather than by an autouse fixture;
    this only owns teardown.
    """
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _write_fresh_snapshot(home: Path, slug: str) -> None:
    path = (
        home / ".scitex" / "agent-container" / "accounts" / slug / ".credentials.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    future_ms = int((time.time() + 7_200.0) * 1_000)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": future_ms}}))


def _make_pinned_config(name: str, account: str) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.account = account
    return cfg


def test_boot_fails_loud_when_cache_present_but_pick_is_blind(
    _isolate_home: Path, tmp_path: Path, _restore_quota_cache_env: None
):
    # Arrange: a token-fresh pinned account, plus a quota cache FILE that
    # exists but carries NO entry for it (a populator that produced nothing).
    home = _isolate_home
    _write_fresh_snapshot(home, "ywatanabe-scitex-ai")
    cache = tmp_path / "present-quota-cache.json"
    cache.write_text(json.dumps({"written_at": 1_784_530_000.0, "accounts": {}}))
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(cache)
    cfg = _make_pinned_config("cards", "ywatanabe-scitex-ai")

    # Act
    # Assert: a present-but-blind cache is a populator failure — boot refuses.
    with pytest.raises(NoHealthyAccountError):
        _rotate_to_healthy_account(cfg, log_stream=io.StringIO())


def test_boot_degrades_and_keeps_pin_when_no_cache_file_exists(
    _isolate_home: Path, tmp_path: Path, _restore_quota_cache_env: None
):
    # Arrange: the same token-fresh pinned account, but NO quota cache file
    # anywhere the reader looks (a fresh / CI / quota-cron-less host).
    home = _isolate_home
    _write_fresh_snapshot(home, "ywatanabe-scitex-ai")
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(tmp_path / "absent-quota-cache.json")
    cfg = _make_pinned_config("cards", "ywatanabe-scitex-ai")

    # Act: a cache-less host must never be blocked on a quota system it lacks.
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())

    # Assert: the pinned account is kept (degrade to freshness-only, no raise).
    assert cfg.claude.account == "ywatanabe-scitex-ai"
