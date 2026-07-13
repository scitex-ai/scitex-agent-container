"""Tests for the auth-failure CAUSE diagnosis — REVOKED vs genuinely EXPIRED.

Claude Code renders every 401 as ``Login expired · Please run /login``. On this
fleet that text is usually FALSE: a sibling agent's OAuth refresh consumes the
single-use ``refresh_token``, rotates the access token, and REVOKES the one this
agent still holds in memory. Nothing expired — and the cure is a restart, not a
login. (Proven 2026-07-13: accounts were valid for another +4h56m..+7h28m and
quota sat at 14% at the exact moment agents were dying.)

The discriminator is ``claudeAiOauth.expiresAt`` on the agent's own credential:

* still in the FUTURE ⇒ that file would authenticate a fresh process right now,
  so a 401 can only mean the in-memory token was taken away ⇒ ``revoked``;
* already in the PAST ⇒ a real ``expired``.

No mocks and no monkeypatch: every test writes a REAL ``.credentials.json`` into
``tmp_path`` and passes that ``tmp_path`` in as the ``home`` collaborator, so the
production resolver runs its REAL logic (including ``account_store._store_path``)
against real bytes on a real filesystem.

TQ: AAA marker triple (TQ002), one asserted fact per test (TQ007).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._account import auth_failure_reason as afr

# ---------------------------------------------------------------------------
# Real collaborators — a config object and a credential file on disk
# ---------------------------------------------------------------------------


class _Claude:
    """The ``spec.claude`` block, reduced to the one field the resolver reads."""

    def __init__(self, account: str) -> None:
        self.account = account


class _Config:
    """A hand-rolled stand-in for AgentConfig: only ``.claude.account`` is read."""

    def __init__(self, account: str = "") -> None:
        self.claude = _Claude(account)


@pytest.fixture
def now() -> float:
    """A fixed reference instant so expiry comparisons are deterministic."""
    return 1_784_000_000.0


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """Where a pinned agent's credential lives under a ``home`` of ``tmp_path``.

    Mirrors ``account_store._store_path``'s real layout; the tests write actual
    bytes here and let production resolve its way to them.
    """
    return (
        tmp_path
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "acct"
        / ".credentials.json"
    )


def _write_creds(path: Path, expires_at_s: float) -> None:
    """Write a REAL credentials.json in claude-code's on-disk format.

    claude-code stores ``expiresAt`` as a unix-MILLISECOND timestamp.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": int(expires_at_s * 1000)}})
    )


# ---------------------------------------------------------------------------
# credential_path_for — which file does this agent authenticate with?
# ---------------------------------------------------------------------------


def test_unpinned_agent_authenticates_with_the_host_live_credential(
    tmp_path: Path,
) -> None:
    # Arrange — no spec.claude.account ⇒ the shared host file.
    config = _Config(account="")
    # Act
    path = afr.credential_path_for(config, home=tmp_path)
    # Assert
    assert path == tmp_path / ".claude" / ".credentials.json"


def test_pinned_agent_authenticates_with_its_account_snapshot(
    tmp_path: Path, snapshot: Path
) -> None:
    # Arrange
    config = _Config(account="acct")
    # Act
    path = afr.credential_path_for(config, home=tmp_path)
    # Assert
    assert path == snapshot


# ---------------------------------------------------------------------------
# diagnose_reason — the decisive comparison
# ---------------------------------------------------------------------------


def test_valid_credential_means_the_token_was_revoked_not_expired(
    tmp_path: Path, snapshot: Path, now: float
) -> None:
    # Arrange — the on-disk credential is still good for another 7 hours, yet the
    # watchdog caught this agent 401-ing. A fresh process would authenticate fine
    # with this very file, so the token it holds in memory was rotated away. This
    # is the 2026-07-13 incident, and the banner's "Login expired" is a lie.
    _write_creds(snapshot, now + 7 * 3600)
    config = _Config(account="acct")
    # Act
    reason = afr.diagnose_reason(config, now=now, home=tmp_path)
    # Assert
    assert reason == afr.REASON_REVOKED


def test_past_expiry_means_the_token_genuinely_expired(
    tmp_path: Path, snapshot: Path, now: float
) -> None:
    # Arrange — nothing on disk can authenticate; this one really did expire.
    _write_creds(snapshot, now - 60)
    config = _Config(account="acct")
    # Act
    reason = afr.diagnose_reason(config, now=now, home=tmp_path)
    # Assert
    assert reason == afr.REASON_EXPIRED


def test_missing_credential_yields_unknown_rather_than_a_guess(
    tmp_path: Path, now: float
) -> None:
    # Arrange — no snapshot written at all. A confidently-wrong cause is exactly
    # what got us here, so the honest answer is "I could not tell".
    config = _Config(account="acct")
    # Act
    reason = afr.diagnose_reason(config, now=now, home=tmp_path)
    # Assert
    assert reason == afr.REASON_UNKNOWN


def test_credential_without_a_numeric_expiry_yields_unknown(
    tmp_path: Path, snapshot: Path, now: float
) -> None:
    # Arrange — a real file, but no usable claudeAiOauth.expiresAt.
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({"claudeAiOauth": {}}))
    config = _Config(account="acct")
    # Act
    reason = afr.diagnose_reason(config, now=now, home=tmp_path)
    # Assert
    assert reason == afr.REASON_UNKNOWN


def test_absent_config_degrades_to_unknown_without_raising(tmp_path: Path, now: float) -> None:
    # Arrange — the watchdog must still annotate a failure when the spec is gone
    # (it then falls back to the host live file, absent under this tmp home); a
    # diagnosis hiccup may never take down the whole fleet view.
    # Act
    reason = afr.diagnose_reason(None, now=now, home=tmp_path)
    # Assert
    assert reason == afr.REASON_UNKNOWN


def test_diagnosis_defaults_now_to_the_wall_clock(tmp_path: Path, snapshot: Path) -> None:
    # Arrange — an expiry 7h in the future must read as revoked with NO injected
    # clock, proving the default `now` path is wired to real time.
    _write_creds(snapshot, time.time() + 7 * 3600)
    config = _Config(account="acct")
    # Act
    reason = afr.diagnose_reason(config, home=tmp_path)
    # Assert
    assert reason == afr.REASON_REVOKED


# ---------------------------------------------------------------------------
# remedy_for — the actionable payoff
# ---------------------------------------------------------------------------


def test_revoked_token_is_cured_by_a_restart() -> None:
    # Arrange — the credential file is already valid; only the process is stale,
    # and Claude Code never re-reads the file. No human needed.
    # Act
    remedy = afr.remedy_for(afr.REASON_REVOKED)
    # Assert
    assert remedy == "restart"


def test_expired_token_needs_an_actual_login() -> None:
    # Arrange — nothing on disk can authenticate; new credentials must be minted.
    # Act
    remedy = afr.remedy_for(afr.REASON_EXPIRED)
    # Assert
    assert remedy == "login"


def test_unknown_cause_recommends_the_cheap_safe_move_first() -> None:
    # Arrange — a restart is harmless and cures the common (revoked) case.
    # Act
    remedy = afr.remedy_for(afr.REASON_UNKNOWN)
    # Assert
    assert remedy == "restart"
