"""Tests for ``runtimes._apptainer_creds`` — per-agent OAuth resolver.

Regression guard for the "boot-snapshot clobbers fresh per-agent
token" bug that recurred fleet-wide: the in-container Claude CLI
refreshes its OAuth ``accessToken`` (~1h cadence) directly on the
per-agent ``:rw`` copy at ``state_dir/claude/.credentials.json``.
Before this guard, every ``sac agent restart`` re-ran
:func:`resolve_cred_file` and unconditionally
``shutil.copy2(snapshot, dest)`` — silently overwriting the freshly
refreshed token with the stale boot-time snapshot. Auth then died at
the next refresh cycle.

No-mocks (PA-306): the tests drive the REAL public function with REAL
files on disk (tmp ``$HOME``, real ``.credentials.json`` JSON bodies,
real ``shutil.copy2``). The OAuth-expiry helper is the production
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
    layout ``sac account save`` writes (``~/.scitex/agent-container/
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


def _write_dest(state_dir: Path, expires_at_seconds: float, token: str) -> Path:
    """Materialise a per-agent dest at the exact path the resolver
    writes (``<state_dir>/claude/.credentials.json``) with the given
    OAuth ``expiresAt`` (seconds, stored as milliseconds on disk)."""
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
# REGRESSION — fresher dest must not be clobbered by a stale snapshot
# ---------------------------------------------------------------------------


def test_fresher_dest_token_is_preserved_when_snapshot_is_older(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — a saved snapshot with a valid (1h-ahead) expiry and a
    # per-agent dest that was refreshed in-container to a much-newer
    # token (24h-ahead). This is the EXACT shape of the recurring auth
    # bug: agent restart re-runs the resolver and (pre-fix)
    # unconditionally copies the older snapshot over the newer dest.
    now = time.time()
    snapshot_expiry = now + 3_600  # +1h — valid, not expired
    dest_expiry = now + 86_400  # +24h — much fresher (post-refresh)
    _write_snapshot(home_redirect, "alpha", snapshot_expiry)
    state_dir = tmp_path / "state"
    fresh_token = "in-container-refreshed-token"
    dest = _write_dest(state_dir, dest_expiry, token=fresh_token)
    # Act — resolver runs at agent restart.
    resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)
    # Assert — the per-agent dest still carries the FRESH in-container
    # token; the resolver did NOT clobber it with the stale snapshot.
    body = json.loads(dest.read_text())
    assert body["claudeAiOauth"]["accessToken"] == fresh_token


def test_missing_dest_is_populated_from_snapshot(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — first-ever start: no dest exists yet, snapshot is valid.
    # (The non-existence of dest is implicit in the empty state_dir;
    # asserting it would be a second assertion and trip TQ007.)
    now = time.time()
    snapshot_expiry = now + 3_600
    snap = _write_snapshot(home_redirect, "alpha", snapshot_expiry)
    state_dir = tmp_path / "state"
    # Act
    resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)
    # Assert — dest now exists with the snapshot's bytes (cold-start path).
    dest = state_dir / "claude" / ".credentials.json"
    assert dest.read_text() == snap.read_text()


def test_older_dest_is_overwritten_when_snapshot_is_newer(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — operator just ran `sac account save alpha` with a
    # freshly-logged-in token; the per-agent dest still holds the
    # older pre-rotation token. On restart, the snapshot SHOULD win
    # because it is strictly newer.
    now = time.time()
    snapshot_expiry = now + 86_400  # +24h — the newer one
    dest_expiry = now + 3_600  # +1h  — older
    snap = _write_snapshot(home_redirect, "alpha", snapshot_expiry)
    state_dir = tmp_path / "state"
    _write_dest(state_dir, dest_expiry, token="stale-pre-rotation-token")
    dest = state_dir / "claude" / ".credentials.json"
    # Act
    resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)
    # Assert — the resolver overwrote dest with the newer snapshot.
    assert dest.read_text() == snap.read_text()


def test_resolver_refuses_expired_snapshot_even_with_fresh_dest(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — pinned-account safety is upstream of the freshness
    # guard: an EXPIRED snapshot must hard-error (PinnedAccountError)
    # regardless of dest state, so the operator is forced to re-login
    # instead of the agent silently running off the per-agent copy.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now - 60)  # already expired
    state_dir = tmp_path / "state"
    _write_dest(state_dir, now + 86_400, token="fresh-but-irrelevant")
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(PinnedAccountError, match="expired"):
        resolve_cred_file(
            _config(tmp_path / "wd", account="alpha"), state_dir, now=now
        )
