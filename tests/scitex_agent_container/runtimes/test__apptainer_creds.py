"""Tests for ``runtimes._apptainer_creds`` — per-agent OAuth resolver.

After operator task #15 (2026-06-01 fleet-wide silent outage fix), the
pinned-account resolver returns the SNAPSHOT FILE ITSELF — not a
boot-copy into the per-agent state-dir. The companion module
:mod:`test__apptainer_creds_rw_bind` covers the new positive contract
(``resolved == snapshot``, no per-agent dest is written, mid-session
refresh writeback through the ``:rw`` bind is visible without restart).

This module keeps the **safety-gate** half of the contract (preserved
from the legacy resolver): pinned agents NEVER silently launch with
an absent / unverifiable / already-expired token. A pinned agent that
cannot reach a healthy snapshot must hard-error so the operator's
remedy path (``claude /login`` + ``sac accounts sync-live`` or
``sac accounts save``) is forced.

No-mocks (PA-306): the tests drive the REAL public function with REAL
files on disk (tmp ``$HOME``, real ``.credentials.json`` JSON bodies).
The OAuth-expiry helper is the production
:func:`_account.creds_sync._read_oauth_expiry_seconds`, exercised
through the resolver — no test-side reimplementation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes._apptainer_creds import (
    PinnedAccountError,
    resolve_cred_file,
)

# ---------------------------------------------------------------------------
# Fixtures — real $HOME, real account-store layout, real credentials JSON
# ---------------------------------------------------------------------------


@pytest.fixture
def home_redirect(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so the resolver's ``Path.home()`` reads from
    an isolated tmp dir. POSIX ``Path.home()`` consults ``$HOME``
    directly, so an env-var swap is sufficient (no monkeypatch)."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _write_snapshot(home: Path, name: str, expires_at_seconds: float) -> Path:
    """Materialise a real saved-account snapshot at the same on-disk
    layout ``sac accounts save`` writes (``~/.scitex/agent-container/
    accounts/<name>/.credentials.json``) with a numeric OAuth
    ``expiresAt`` in MILLISECONDS (matching the claude-code wire
    format). Returns the snapshot path."""
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / name
    acct_dir.mkdir(parents=True, exist_ok=True)
    snap = acct_dir / ".credentials.json"
    body = {
        "claudeAiOauth": {
            "accessToken": f"snapshot-token-{int(expires_at_seconds)}",
            "expiresAt": int(expires_at_seconds * 1_000),
        }
    }
    snap.write_text(json.dumps(body))
    return snap


def _write_legacy_dest(state_dir: Path, expires_at_seconds: float, token: str) -> Path:
    """Materialise a per-agent legacy dest at the exact path the OLD
    resolver used to write (``<state_dir>/claude/.credentials.json``).
    Used to prove the new resolver leaves leftover legacy state alone
    rather than touching it."""
    dest = state_dir / "claude" / ".credentials.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int(expires_at_seconds * 1_000),
        }
    }
    dest.write_text(json.dumps(body))
    return dest


def _config(workdir: Path, *, account: str) -> AgentConfig:
    return AgentConfig(
        name="alpha",
        runtime="apptainer",
        workdir=str(workdir),
        claude=ClaudeSpec(account=account),
    )


# ---------------------------------------------------------------------------
# Safety gate — pinned snapshot must be healthy or hard-error
# ---------------------------------------------------------------------------


def test_resolver_refuses_expired_snapshot(tmp_path: Path, home_redirect: Path) -> None:
    # Arrange — pinned-account safety: an EXPIRED snapshot must
    # hard-error (PinnedAccountError) so the operator is forced to
    # re-login instead of the agent silently running off a dead token.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now - 60)  # already expired
    state_dir = tmp_path / "state"
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(PinnedAccountError, match="expired"):
        resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)


# ---------------------------------------------------------------------------
# Legacy tolerance — leftover per-agent dest from the OLD resolver
# must not surprise an operator. The new resolver does not WRITE to
# the legacy dest (covered in test__apptainer_creds_rw_bind), and a
# pre-existing dest is NEITHER read nor mutated by the resolver. The
# bind target is the snapshot regardless of dest state.
# ---------------------------------------------------------------------------


def test_legacy_dest_is_not_touched_by_resolver(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — operator upgrades to the :rw bind fix; the per-agent
    # state dir still carries a stale dest from the pre-fix runtime.
    # The new resolver must leave it ALONE — not read, not overwrite,
    # not mutate. The bind target is the snapshot itself.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now + 3_600)
    state_dir = tmp_path / "state"
    dest = _write_legacy_dest(state_dir, now + 86_400, token="legacy-leftover")
    dest_bytes_before = dest.read_bytes()
    # Act
    resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)
    # Assert — the legacy dest is byte-identical post-resolver.
    assert dest.read_bytes() == dest_bytes_before
