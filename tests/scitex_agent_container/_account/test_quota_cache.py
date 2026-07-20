"""Tests for :mod:`scitex_agent_container._account.quota_cache`.

Coverage: real I/O against tmp fixture files (no mocks). Verifies the
shared reader used by:
  * the a2a metadata enricher in ``_mcp/_channel_tools._wrap_message_send``
  * the ``sac account quota`` CLI helper (agent self-awareness)

Both consumers go through ``read_quota_entry`` / ``build_a2a_metadata``
— the shape is therefore identical to the TS bridge's
``signature.ts:readQuotaEntry``, and these tests pin the contract.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA
markers in every test body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._account.quota_cache import (
    DEFAULT_QUOTA_CACHE_PATH,
    ENV_ACCOUNT,
    ENV_QUOTA_CACHE_PATH,
    META_KEY_ACCOUNT,
    META_KEY_PCT_5H,
    META_KEY_PCT_7D,
    META_KEY_TTL_H,
    _first_existing,
    build_a2a_metadata,
    host_cache_candidates,
    read_quota_entry,
)

# ---------------------------------------------------------------------------
# Fixtures — schema matches the lead's confirmed wire shape (2026-06-02).
# ---------------------------------------------------------------------------

SAMPLE = {
    "written_at": 1780352404.82,
    "accounts": {
        "alpha@example.com": {
            "short": "alpha",
            "h5": 17.0,
            "d7": 3.0,
            "ttl_h": 7.74,
        },
        "beta@example.com": {
            "short": "beta",
            "h5": 11.0,
            "d7": 2.0,
            "ttl_h": 6.52,
        },
        "ywatanabe@scitex.ai": {
            "short": "ywatanabe",
            "h5": 19.0,
            "d7": 3.0,
            "ttl_h": 7.74,
        },
    },
}


@pytest.fixture
def cache_file(tmp_path: Path) -> Path:
    """Drop SAMPLE at a tmp path; return the path."""
    p = tmp_path / "quota-cache.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    return p


@pytest.fixture
def clean_env(env_save_restore) -> None:
    """Each test starts with a known-empty quota-cache env state."""
    env_save_restore.delete(ENV_ACCOUNT)
    env_save_restore.delete(ENV_QUOTA_CACHE_PATH)


# ---------------------------------------------------------------------------
# read_quota_entry — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,expected",
    [
        ("short", "beta"),
        ("h5", 11.0),
        ("d7", 2.0),
        ("ttl_h", 6.52),
    ],
)
def test_read_quota_entry_short_match_returns_field(
    cache_file, clean_env, field: str, expected
) -> None:
    # Arrange
    dirname = "beta-example-com"
    # Act
    entry = read_quota_entry(account=dirname, cache_path=cache_file)
    # Assert
    assert (
        entry is not None and entry[field] == pytest.approx(expected)
        if isinstance(expected, float)
        else entry is not None and entry[field] == expected
    )


def test_read_quota_entry_handles_multi_dot_tld(cache_file, clean_env) -> None:
    # Arrange — ywatanabe-scitex-ai → short=ywatanabe. Domain side has
    # TWO dashes — the lookup must not be fooled by hyphen counting.
    dirname = "ywatanabe-scitex-ai"
    # Act
    entry = read_quota_entry(account=dirname, cache_path=cache_file)
    # Assert
    assert entry is not None and entry["short"] == "ywatanabe"


def test_read_quota_entry_reads_account_from_env(
    cache_file, clean_env, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set(ENV_ACCOUNT, "alpha-example-com")
    # Act
    entry = read_quota_entry(cache_path=cache_file)
    # Assert
    assert entry is not None and entry["short"] == "alpha"


def test_read_quota_entry_reads_cache_path_from_env(
    cache_file, clean_env, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set(ENV_ACCOUNT, "alpha-example-com")
    env_save_restore.set(ENV_QUOTA_CACHE_PATH, str(cache_file))
    # Act
    entry = read_quota_entry()
    # Assert
    assert entry is not None and entry["short"] == "alpha"


def test_read_quota_entry_default_path_constant_is_stable() -> None:
    # Arrange — the apptainer runtime binds to this exact in-container
    # path; PR-A (telegrammer) defaults to the SAME container path via
    # env. A rename without coordinating all three sites would silently
    # break #16.
    constant = DEFAULT_QUOTA_CACHE_PATH
    # Act
    actual = str(constant)
    # Assert
    assert actual == "/var/sac/quota-cache.json"


# ---------------------------------------------------------------------------
# read_quota_entry — failure modes (all collapse to None, never raise)
# ---------------------------------------------------------------------------


def test_read_quota_entry_none_when_account_unset(cache_file, clean_env) -> None:
    # Arrange — fixture present but no account dirname provided.
    path = cache_file
    # Act
    entry = read_quota_entry(cache_path=path)
    # Assert
    assert entry is None


def test_read_quota_entry_none_on_missing_file(tmp_path: Path, clean_env) -> None:
    # Arrange
    nope = tmp_path / "does-not-exist.json"
    # Act
    entry = read_quota_entry(account="alpha-example-com", cache_path=nope)
    # Assert
    assert entry is None


def test_read_quota_entry_none_on_malformed_json(tmp_path: Path, clean_env) -> None:
    # Arrange
    bad = tmp_path / "bad.json"
    bad.write_text("this is not valid json {", encoding="utf-8")
    # Act
    entry = read_quota_entry(account="alpha-example-com", cache_path=bad)
    # Assert
    assert entry is None


def test_read_quota_entry_none_when_no_matching_short(cache_file, clean_env) -> None:
    # Arrange — dirname that splits to a short not in the cache.
    dirname = "nope-no-such"
    # Act
    entry = read_quota_entry(account=dirname, cache_path=cache_file)
    # Assert
    assert entry is None


def test_read_quota_entry_none_on_wrong_typed_fields(tmp_path: Path, clean_env) -> None:
    # Arrange — h5 is a string, not a number.
    bad = tmp_path / "wrong-types.json"
    bad.write_text(
        json.dumps(
            {
                "accounts": {
                    "x@y.z": {
                        "short": "alpha",
                        "h5": "lots",
                        "d7": 3,
                        "ttl_h": 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    # Act
    entry = read_quota_entry(account="alpha-example-com", cache_path=bad)
    # Assert
    assert entry is None


def test_read_quota_entry_rejects_bool_as_percentage(tmp_path: Path, clean_env) -> None:
    # Arrange — bool is int in Python; explicit rejection prevents True
    # silently surfacing as 1.0% utilization.
    bad = tmp_path / "bool.json"
    bad.write_text(
        json.dumps(
            {
                "accounts": {
                    "x@y.z": {
                        "short": "alpha",
                        "h5": True,
                        "d7": 3.0,
                        "ttl_h": 1.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    # Act
    entry = read_quota_entry(account="alpha-example-com", cache_path=bad)
    # Assert
    assert entry is None


def test_read_quota_entry_none_on_non_dict_accounts(tmp_path: Path, clean_env) -> None:
    # Arrange — top-level `accounts` is a list (legacy / future shape).
    bad = tmp_path / "list.json"
    bad.write_text(json.dumps({"accounts": [{"short": "alpha"}]}), encoding="utf-8")
    # Act
    entry = read_quota_entry(account="alpha-example-com", cache_path=bad)
    # Assert
    assert entry is None


def test_read_quota_entry_explicit_account_beats_env(
    cache_file, clean_env, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set(ENV_ACCOUNT, "alpha-example-com")
    # Act — explicit kwarg overrides env.
    entry = read_quota_entry(account="beta-example-com", cache_path=cache_file)
    # Assert
    assert entry is not None and entry["short"] == "beta"


def test_read_quota_entry_returns_copy_not_reference(cache_file, clean_env) -> None:
    # Arrange — mutate the first returned dict; the next read must
    # surface a fresh value. The a2a path tags additional fields onto
    # the dict before forwarding to peers — a shared-reference bug
    # would surface those mutations to subsequent sends. The first
    # read's non-None-ness is guaranteed by the fixture shape and the
    # surrounding happy-path tests; reach in via ``or {}`` so a future
    # regression there fails through the actual under-test assertion
    # below instead of an unrelated precondition assert.
    first = read_quota_entry(account="alpha-example-com", cache_path=cache_file) or {}
    first["short"] = "mutated"
    # Act
    second = read_quota_entry(account="alpha-example-com", cache_path=cache_file) or {}
    # Assert
    assert second.get("short") == "alpha"


# ---------------------------------------------------------------------------
# build_a2a_metadata — shape contract for the a2a wire path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        (META_KEY_ACCOUNT, "alpha"),
        (META_KEY_PCT_5H, 17.0),
        (META_KEY_PCT_7D, 3.0),
        (META_KEY_TTL_H, 7.74),
    ],
)
def test_build_a2a_metadata_field(
    cache_file,
    clean_env,
    env_save_restore,
    key: str,
    expected,
) -> None:
    # Arrange
    env_save_restore.set(ENV_ACCOUNT, "alpha-example-com")
    env_save_restore.set(ENV_QUOTA_CACHE_PATH, str(cache_file))
    # Act
    meta = build_a2a_metadata()
    # Assert
    assert (
        meta[key] == pytest.approx(expected)
        if isinstance(expected, float)
        else meta[key] == expected
    )


def test_build_a2a_metadata_emits_exactly_four_keys(
    cache_file, clean_env, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set(ENV_ACCOUNT, "alpha-example-com")
    env_save_restore.set(ENV_QUOTA_CACHE_PATH, str(cache_file))
    expected_keys = {
        META_KEY_ACCOUNT,
        META_KEY_PCT_5H,
        META_KEY_PCT_7D,
        META_KEY_TTL_H,
    }
    # Act
    meta = build_a2a_metadata()
    # Assert
    assert set(meta.keys()) == expected_keys


@pytest.mark.parametrize(
    "name,expected",
    [
        ("META_KEY_ACCOUNT", "account"),
        ("META_KEY_PCT_5H", "used_pct_5h"),
        ("META_KEY_PCT_7D", "used_pct_7d"),
        ("META_KEY_TTL_H", "token_ttl_hours"),
    ],
)
def test_build_a2a_metadata_canonical_key_name(name: str, expected: str) -> None:
    # Arrange — the TS bridge (PR-A) and Python helper emit the SAME
    # key names so peer-side consumers can do `meta.get("used_pct_5h")`
    # regardless of who sent the message. Pin each name — a rename is
    # then a coordinated change across both repos.
    module = __import__(
        "scitex_agent_container._account.quota_cache",
        fromlist=[name],
    )
    # Act
    actual = getattr(module, name)
    # Assert
    assert actual == expected


def test_host_cache_candidates_puts_runtime_convention_path_first(
    tmp_path: Path,
) -> None:
    # Arrange — sac quota cache is sac runtime state; canonical home is the
    # per-package runtime dir, NOT the shared ~/.scitex root (constitution §3;
    # operator 2026-07-11). The 2026-07-11 incident was the reader ignoring
    # this host path entirely and reading "?" on every host-side `sac-start`.
    home = tmp_path
    # Act
    candidates = host_cache_candidates(home)
    # Assert
    assert (
        candidates[0]
        == home / ".scitex" / "agent-container" / "runtime" / "quota-cache.json"
    )


def test_host_cache_candidates_keeps_legacy_path_as_backcompat_last(
    tmp_path: Path,
) -> None:
    # Arrange — the old top-level path stays readable during migration so a
    # not-yet-moved populator's file still resolves.
    home = tmp_path
    # Act
    candidates = host_cache_candidates(home)
    # Assert
    assert candidates[-1] == home / ".scitex" / "quota-cache.json"


def test_first_existing_returns_runtime_path_over_legacy(tmp_path: Path) -> None:
    # Arrange — both host files present; the runtime canonical must win.
    runtime = tmp_path / ".scitex" / "agent-container" / "runtime" / "quota-cache.json"
    legacy = tmp_path / ".scitex" / "quota-cache.json"
    for p in (runtime, legacy):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    # Act
    chosen = _first_existing(host_cache_candidates(tmp_path))
    # Assert
    assert chosen == runtime


def test_first_existing_falls_back_to_legacy_when_only_legacy_present(
    tmp_path: Path,
) -> None:
    # Arrange — only the legacy file exists (populator not yet migrated).
    legacy = tmp_path / ".scitex" / "quota-cache.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(SAMPLE), encoding="utf-8")
    # Act
    chosen = _first_existing(host_cache_candidates(tmp_path))
    # Assert
    assert chosen == legacy


def test_first_existing_returns_none_when_no_host_file(tmp_path: Path) -> None:
    # Arrange — a bare home with neither host file (fresh machine / CI).
    # Act
    chosen = _first_existing(host_cache_candidates(tmp_path))
    # Assert
    assert chosen is None


def test_build_a2a_metadata_empty_dict_when_no_entry(clean_env, tmp_path: Path) -> None:
    # Arrange — no env, no file.
    # Act
    meta = build_a2a_metadata()
    # Assert
    assert meta == {}


def test_build_a2a_metadata_empty_dict_does_not_corrupt_outer_metadata(
    clean_env,
) -> None:
    # Arrange — empty dict so callers can `metadata.update(...)` without
    # leaking None-valued fields onto the wire.
    outer = {"from_agent": "alice", "conversation_id": "c1"}
    # Act
    outer.update(build_a2a_metadata())
    # Assert
    assert outer == {"from_agent": "alice", "conversation_id": "c1"}
