"""Tests for the SPEC-AWARE half of the start preflight.

The gate must read the credentials the agent will ACTUALLY authenticate
with — the ``claude.credentials_files`` pool, the singular
``claude.credentials_file``, or the ``claude.account`` snapshot — and
only fall back to the lead's ``~/.claude/.credentials.json`` when the
spec declares none of them.

The regression these pin is the 2026-08-10 host outage: the lead file
had lapsed ~16h earlier, every pool credential the fleet's specs declare
was fresh, twelve agents were running on them, and ``sac agents start``
refused EVERY start on the host because the gate only ever looked at the
lead file. A false negative, not a safety net.

Style rules in force here (mirroring ``test__preflight_creds.py``):
* One assert per test (STX-TQ007).
* AAA markers each on their own line.
* No monkeypatch / mocker fixture params (STX-NM002) — env mutation
  uses ``os.environ`` save/restore in a fixture.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state._preflight_creds import (
    check_spec_oauth_credentials,
    spec_credential_candidates,
)
from scitex_agent_container.config import AgentConfig

_FROZEN_NOW = 1_700_000_000.0
_FROZEN_NOW_MS = 1_700_000_000_000
_ONE_HOUR_MS = 3_600_000


def _write_creds(path: Path, expires_at_ms: int) -> Path:
    """Materialise a ``.credentials.json`` with the given ``expiresAt``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _fresh(path: Path) -> Path:
    return _write_creds(path, _FROZEN_NOW_MS + _ONE_HOUR_MS)


def _stale(path: Path) -> Path:
    return _write_creds(path, _FROZEN_NOW_MS - _ONE_HOUR_MS)


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Strip the API-key env vars so the OAuth branch is actually taken."""
    # Arrange
    snapshot = {
        "ANTHROPIC_API_KEY": os.environ.pop("ANTHROPIC_API_KEY", None),
        "SAC_ANTHROPIC_API_KEY": os.environ.pop("SAC_ANTHROPIC_API_KEY", None),
    }
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def expired_lead_home(tmp_path: Path, clean_env) -> Iterator[Path]:
    """Pin ``$HOME`` to a tmp dir holding an EXPIRED lead credentials file.

    This is the outage's shape: the fallback artefact is dead. Any test
    that passes under this fixture proves the gate did not consult it.
    """
    # Arrange
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    _stale(tmp_path / ".claude" / ".credentials.json")
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _pool_config(name: str, paths: list[Path]) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.credentials_files = [str(p) for p in paths]
    return cfg


# ---------------------------------------------------------------------------
# The live bug: a declared pool credential is fresh, the lead file is not.
# ---------------------------------------------------------------------------


class TestDeclaredPoolWins:
    def test_valid_pool_entry_passes_while_lead_file_is_expired(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        good = _fresh(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        cfg = _pool_config("dotfiles", [good])
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        assert picked == good

    def test_second_pool_entry_rescues_an_expired_first(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        dead = _stale(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        good = _fresh(expired_lead_home / "accounts" / "beta" / ".credentials.json")
        cfg = _pool_config("dotfiles", [dead, good])
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        assert picked == good

    def test_singular_credentials_file_is_honoured(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        good = _fresh(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.credentials_file = str(good)
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        assert picked == good

    def test_account_snapshot_is_honoured(self, tmp_path: Path, clean_env) -> None:
        # Arrange
        store = tmp_path / ".scitex" / "agent-container" / "accounts"
        good = _fresh(store / "ywata1989-gmail-com" / ".credentials.json")
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.account = "ywata1989-gmail-com"
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW, home=tmp_path)
        # Assert
        assert picked == good


# ---------------------------------------------------------------------------
# Still an ERROR, never a warning — but about the RIGHT files.
# ---------------------------------------------------------------------------


class TestEveryCandidateFails:
    def test_all_pool_entries_expired_refuses(self, expired_lead_home: Path) -> None:
        # Arrange
        dead_a = _stale(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        dead_b = _stale(expired_lead_home / "accounts" / "beta" / ".credentials.json")
        cfg = _pool_config("dotfiles", [dead_a, dead_b])
        # Act
        action = lambda: check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(RuntimeError, match="every credential its spec declares"):
            action()

    def test_refusal_names_every_candidate_not_just_the_first(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        dead = _stale(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        missing = expired_lead_home / "accounts" / "beta" / ".credentials.json"
        cfg = _pool_config("dotfiles", [dead, missing])
        # Act
        try:
            check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
            message = ""
        except RuntimeError as exc:
            message = str(exc)
        # Assert
        assert str(dead) in message and str(missing) in message

    def test_refusal_states_why_each_candidate_failed(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        dead = _stale(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        missing = expired_lead_home / "accounts" / "beta" / ".credentials.json"
        cfg = _pool_config("dotfiles", [dead, missing])
        # Act
        try:
            check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
            message = ""
        except RuntimeError as exc:
            message = str(exc)
        # Assert
        assert "expired" in message and "not found" in message

    def test_undeclared_spec_still_refuses_on_an_expired_lead_file(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        cfg = AgentConfig(name="unpinned")
        # Act
        action = lambda: check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(RuntimeError, match=r"expired \d+ seconds ago"):
            action()


# ---------------------------------------------------------------------------
# API-key bypass — unchanged, and it must win over every candidate.
# ---------------------------------------------------------------------------


class TestApiKeyBypass:
    def test_api_key_env_skips_the_spec_check(self, expired_lead_home: Path) -> None:
        # Arrange
        dead = _stale(expired_lead_home / "accounts" / "alpha" / ".credentials.json")
        cfg = _pool_config("dotfiles", [dead])
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        assert picked is None

    def test_sac_api_key_env_skips_the_spec_check(
        self, expired_lead_home: Path
    ) -> None:
        # Arrange
        cfg = AgentConfig(name="unpinned")
        os.environ["SAC_ANTHROPIC_API_KEY"] = "sk-ant-fake"
        # Act
        picked = check_spec_oauth_credentials(cfg, now=_FROZEN_NOW)
        # Assert
        assert picked is None


# ---------------------------------------------------------------------------
# Candidate resolution mirrors the runtime's own precedence.
# ---------------------------------------------------------------------------


class TestCandidateResolution:
    def test_plural_pool_outranks_the_singular_field(self, tmp_path: Path) -> None:
        # Arrange
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.credentials_files = [str(tmp_path / "pool.json")]
        cfg.claude.credentials_file = str(tmp_path / "single.json")
        # Act
        candidates, _declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert [p for _o, p in candidates] == [tmp_path / "pool.json"]

    def test_pool_outranks_a_named_account_pin(self, tmp_path: Path) -> None:
        # Arrange
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.credentials_files = [str(tmp_path / "pool.json")]
        cfg.claude.account = "alpha"
        # Act
        candidates, _declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert [p for _o, p in candidates] == [tmp_path / "pool.json"]

    def test_pool_order_is_the_declared_order(self, tmp_path: Path) -> None:
        # Arrange
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.credentials_files = [
            str(tmp_path / "b.json"),
            str(tmp_path / "a.json"),
        ]
        # Act
        candidates, _declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert [p.name for _o, p in candidates] == ["b.json", "a.json"]

    def test_each_candidate_carries_its_spec_field_origin(self, tmp_path: Path) -> None:
        # Arrange
        cfg = AgentConfig(name="dotfiles")
        cfg.claude.credentials_files = [
            str(tmp_path / "a.json"),
            str(tmp_path / "b.json"),
        ]
        # Act
        candidates, _declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert [o for o, _p in candidates] == [
            "spec.claude.credentials_files[0]",
            "spec.claude.credentials_files[1]",
        ]

    def test_an_undeclared_spec_reports_declared_false(self, tmp_path: Path) -> None:
        # Arrange
        cfg = AgentConfig(name="unpinned")
        # Act
        _candidates, declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert declared is False

    def test_an_undeclared_spec_falls_back_to_the_lead_file(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        cfg = AgentConfig(name="unpinned")
        # Act
        candidates, _declared = spec_credential_candidates(cfg.claude, home=tmp_path)
        # Assert
        assert [p for _o, p in candidates] == [
            tmp_path / ".claude" / ".credentials.json"
        ]
