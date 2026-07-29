"""The blind-pick remedy reports only what it OBSERVED about the cache.

``quota_cache_entry_count`` returns 0 for four different worlds — absent,
unreadable, malformed, and genuinely empty. The remedy used to branch on
``== 0`` and then assert a cause it had never measured: "the cache exists but
holds ZERO account entries, so the populator has never written a successful
one". On 2026-07-29 the operator saw that while the cron populator was writing
three accounts every five minutes and the file held them; they re-ran the
refresh twice on its advice and nothing changed.

These lock the discrimination (a remedy must name a MEASURED cause, not a
direction) and the one detail that would have ended that investigation in a
line: every branch names the file it actually read.

PA-306: no mocks — real cache files on disk, real reader resolution.
AAA markers (TQ002); descriptive names; one assertion each (TQ007).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds._pick_healthy import _blind_cache_remedy


@pytest.fixture
def _restore_quota_cache_env() -> Iterator[None]:
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _write_cache(path: Path, accounts: dict) -> Path:
    path.write_text(
        json.dumps({"written_at": 0.0, "accounts": accounts}), encoding="utf-8"
    )
    return path


def _write_malformed(path: Path) -> Path:
    """Truncated JSON at ``path``.

    A named helper rather than a lambda: ``write_text`` returns the CHARACTER
    COUNT, so ``p.write_text(...) or p`` evaluates to ``1`` and the parametrised
    case silently passed an int where a path belonged.
    """
    path.write_text("{", encoding="utf-8")
    return path


def test_populated_cache_is_never_blamed_on_an_unrun_populator(
    tmp_path: Path,
) -> None:
    # Arrange — the operator's real 2026-07-29 state: a cache the cron
    # populator had just written, holding three accounts.
    cache = _write_cache(
        tmp_path / "quota-cache.json",
        {
            "wyusuuke-gmail-com": {"h5": 5.0, "d7": 58.0},
            "ywata1989-gmail-com": {"h5": 21.0, "d7": 20.0},
            "ywatanabe-scitex-ai": {"h5": 28.0, "d7": 42.0},
        },
    )

    # Act
    remedy = _blind_cache_remedy(cache)

    # Assert — the sentence that cost the operator two retries must not appear
    # for a cache that demonstrably holds entries.
    assert "populator has never written a successful one" not in remedy


def test_populated_cache_reports_the_entry_count_it_found(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(
        tmp_path / "quota-cache.json",
        {"a-com": {"h5": 1.0}, "b-com": {"h5": 2.0}, "c-com": {"h5": 3.0}},
    )

    # Act
    remedy = _blind_cache_remedy(cache)

    # Assert
    assert "holds 3 account entries" in remedy


def test_populated_cache_points_at_an_account_set_mismatch(tmp_path: Path) -> None:
    # Arrange — entries exist but none covers a fresh candidate, which is the
    # branch the operator SHOULD have been shown.
    cache = _write_cache(tmp_path / "quota-cache.json", {"only-com": {"h5": 1.0}})

    # Act
    remedy = _blind_cache_remedy(cache)

    # Assert
    assert "different account set" in remedy


def test_absent_cache_is_not_described_as_existing(tmp_path: Path) -> None:
    # Arrange — nothing written at the consulted path.
    missing = tmp_path / "quota-cache.json"

    # Act
    remedy = _blind_cache_remedy(missing)

    # Assert
    assert "NO quota cache exists" in remedy


def test_unreadable_cache_is_not_blamed_on_the_populator(tmp_path: Path) -> None:
    # Arrange — a directory at the cache path: it EXISTS (so present() is True
    # and the gate arms) but reading it raises OSError, the state a
    # present/count pair cannot see. A directory reproduces this for any uid,
    # unlike chmod 000, which root ignores.
    blocked = tmp_path / "quota-cache.json"
    blocked.mkdir()

    # Act
    remedy = _blind_cache_remedy(blocked)

    # Assert
    assert "could NOT be read" in remedy


def test_malformed_cache_is_not_described_as_zero_entries(tmp_path: Path) -> None:
    # Arrange — valid file, invalid JSON (a truncated or hand-edited cache).
    corrupt = tmp_path / "quota-cache.json"
    corrupt.write_text('{"written_at": 0.0, "accou', encoding="utf-8")

    # Act
    remedy = _blind_cache_remedy(corrupt)

    # Assert
    assert "not valid cache JSON" in remedy


def test_wrong_shaped_cache_is_treated_as_malformed(tmp_path: Path) -> None:
    # Arrange — parses as JSON, but "accounts" is not a mapping.
    wrong = tmp_path / "quota-cache.json"
    wrong.write_text('{"written_at": 0.0, "accounts": []}', encoding="utf-8")

    # Act
    remedy = _blind_cache_remedy(wrong)

    # Assert
    assert "not valid cache JSON" in remedy


def test_empty_cache_still_names_zero_entries(tmp_path: Path) -> None:
    # Arrange — the one state the old ZERO wording was actually correct for.
    cache = _write_cache(tmp_path / "quota-cache.json", {})

    # Act
    remedy = _blind_cache_remedy(cache)

    # Assert
    assert "holds ZERO account entries" in remedy


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda p: p, id="absent"),
        pytest.param(lambda p: p.mkdir() or p, id="unreadable"),
        pytest.param(_write_malformed, id="malformed"),
        pytest.param(lambda p: _write_cache(p, {}), id="empty"),
        pytest.param(lambda p: _write_cache(p, {"a-com": {"h5": 1.0}}), id="populated"),
    ],
)
def test_every_branch_names_the_file_it_read(tmp_path: Path, build) -> None:
    # Arrange — "which file did it actually read" was unanswerable from the old
    # message, and answering it is what ends this class of investigation.
    cache = build(tmp_path / "quota-cache.json")

    # Act
    remedy = _blind_cache_remedy(cache)

    # Assert
    assert str(cache) in remedy


def test_env_resolved_path_is_reported_when_no_explicit_path_is_passed(
    tmp_path: Path, _restore_quota_cache_env: None
) -> None:
    # Arrange — the real boot path passes None and lets the reader resolve; the
    # operator's failure gave no way to tell WHICH file that landed on.
    cache = _write_cache(tmp_path / "quota-cache.json", {})
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(cache)

    # Act
    remedy = _blind_cache_remedy(None)

    # Assert
    assert str(cache) in remedy
