"""Writable bind of the live per-account credential file (operator #15).

Root cause of the 2026-06-01 fleet-wide silent outage (operator brief):
agents pinned via ``spec.claude.account`` got a FROZEN BOOT-COPY of the
saved account's snapshot in their state dir. When the in-container
Claude CLI refreshed the OAuth ``accessToken`` (~1h cadence), the refresh
landed on that per-agent copy — not on the source snapshot. After the
~8h refresh-token TTL elapsed (or the per-agent copy drifted across
restarts), every SDK turn 401'd. The telegram bridge still marked
inbound 👀, but the agent could not complete a turn → silent. Hit the
hub and multiple project agents (revived only by restart).

Fix (operator-approved): mount the per-account live credential file
DIRECTLY as a ``:rw`` bind. The agent always reads the latest token AND
OAuth refresh writes back to the same file → the source snapshot is
self-healing and never expires while the in-container CLI keeps
refreshing.

This module is the TDD proof that the new resolver returns the
**snapshot path itself** (not a per-agent copy), so the existing
``:rw`` bind in :mod:`runtimes._apptainer_auth` lands on the snapshot
and refresh writes back. The companion :mod:`test__apptainer_auth`
asserts the bind is actually emitted as ``:rw``.

NO MOCKS (PA-306): real ``$HOME`` redirect, real on-disk snapshot, real
OAuth-expiry resolution via the production
:func:`_account.creds_sync._read_oauth_expiry_seconds`. Each test:
AAA markers (TQ002), one assertion (TQ007), 3+-word name.
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
# Fixtures + helpers (mirrors test__apptainer_creds.py shape)
# ---------------------------------------------------------------------------


@pytest.fixture
def home_redirect(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so ``Path.home()`` reads from tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _write_snapshot(
    home: Path, name: str, expires_at_seconds: float, *, token: str = "tok"
) -> Path:
    """Materialise a real saved-account snapshot at the same on-disk
    layout ``sac accounts save`` writes."""
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / name
    acct_dir.mkdir(parents=True, exist_ok=True)
    snap = acct_dir / ".credentials.json"
    body = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int(expires_at_seconds * 1_000),
        }
    }
    snap.write_text(json.dumps(body))
    return snap


def _config(workdir: Path, *, account: str) -> AgentConfig:
    return AgentConfig(
        name="alpha",
        runtime="apptainer",
        workdir=str(workdir),
        claude=ClaudeSpec(account=account),
    )


# ---------------------------------------------------------------------------
# Pinned-account branch — resolver returns the LIVE snapshot path
# ---------------------------------------------------------------------------


def test_pinned_returns_snapshot_path_itself_not_per_agent_copy(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — a healthy pinned snapshot. The state_dir is what the OLD
    # implementation used to write a boot-copy into; the new resolver
    # must IGNORE it for the bind target and hand back the snapshot.
    now = time.time()
    snap = _write_snapshot(home_redirect, "alpha", now + 3_600)
    state_dir = tmp_path / "state"
    # Act
    resolved = resolve_cred_file(
        _config(tmp_path / "wd", account="alpha"), state_dir, now=now
    )
    # Assert — the bind target IS the snapshot (not a per-agent copy).
    # Refresh writes by the in-container CLI land on the snapshot
    # directly, which is the whole point of the :rw bind fix.
    assert resolved == snap


def test_pinned_does_not_create_per_agent_dest_copy(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — fresh start; the legacy ``<state_dir>/claude/.credentials.json``
    # MUST NOT be written by the resolver, otherwise a future operator
    # could be confused by a stale-looking file under runtime/<name>/.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now + 3_600)
    state_dir = tmp_path / "state"
    # Act
    resolve_cred_file(_config(tmp_path / "wd", account="alpha"), state_dir, now=now)
    # Assert — no per-agent copy materialised under state_dir.
    legacy_dest = state_dir / "claude" / ".credentials.json"
    assert not legacy_dest.exists()


def test_pinned_refresh_writeback_visible_via_resolved_path(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — the lead's acceptance test: "refresh the source cred
    # file mid-session and confirm the next turn picks it up". Drive
    # this at the resolver level: an external mutation of the snapshot
    # (simulating the in-container CLI's refresh writeback through the
    # :rw bind) must be visible immediately via the path the resolver
    # returned — because the resolver returned the snapshot ITSELF, not
    # a copy.
    now = time.time()
    snap = _write_snapshot(home_redirect, "alpha", now + 3_600, token="boot-token")
    state_dir = tmp_path / "state"
    resolved = resolve_cred_file(
        _config(tmp_path / "wd", account="alpha"), state_dir, now=now
    )
    # Act — simulate the in-container Claude CLI refreshing the OAuth
    # token: rewrite the snapshot's content (atomic in production via
    # the CLI's own tmp+rename; here a direct write is sufficient for
    # the unit-level invariant).
    new_body = {
        "claudeAiOauth": {
            "accessToken": "refreshed-mid-session-token",
            "expiresAt": int((now + 86_400) * 1_000),
        }
    }
    snap.write_text(json.dumps(new_body))
    # Assert — the path the resolver handed out NOW reads the refreshed
    # token. No restart, no re-resolve needed — it's the same file.
    body = json.loads(resolved.read_text())
    assert body["claudeAiOauth"]["accessToken"] == "refreshed-mid-session-token"


# ---------------------------------------------------------------------------
# Pinned-account safety gates (preserved from the legacy resolver)
# ---------------------------------------------------------------------------


def test_pinned_absent_snapshot_raises_pinned_account_error(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — pinned to an account whose store dir does not exist.
    state_dir = tmp_path / "state"
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(PinnedAccountError, match="has no credential snapshot"):
        resolve_cred_file(
            _config(tmp_path / "wd", account="missing-acct"),
            state_dir,
            now=time.time(),
        )


def test_pinned_expired_snapshot_raises_pinned_account_error(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — pinned snapshot whose OAuth ``expiresAt`` is already in
    # the past. The resolver must refuse rather than handing out a
    # dead token (the broken-fleet failure mode).
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now - 60)
    state_dir = tmp_path / "state"
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(PinnedAccountError, match="expired"):
        resolve_cred_file(
            _config(tmp_path / "wd", account="alpha"),
            state_dir,
            now=now,
        )


def test_pinned_snapshot_missing_expiry_field_raises(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — pinned snapshot with no numeric ``expiresAt``: must
    # hard-error (we will not launch a pinned agent with an
    # unverifiable token).
    acct_dir = home_redirect / ".scitex" / "agent-container" / "accounts" / "alpha"
    acct_dir.mkdir(parents=True)
    (acct_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    state_dir = tmp_path / "state"
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(PinnedAccountError, match="expiresAt"):
        resolve_cred_file(
            _config(tmp_path / "wd", account="alpha"),
            state_dir,
            now=time.time(),
        )


# ---------------------------------------------------------------------------
# Unpinned-account branch (acct="") — unchanged: host live file
# ---------------------------------------------------------------------------


def test_unpinned_returns_host_live_credentials_json(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — no pinned account; host live ``~/.claude/.credentials.json``
    # is the resolver's answer (caller binds it ``:rw`` so refresh-in-place
    # works on the same file the host CLI reads/writes).
    host_claude = home_redirect / ".claude"
    host_claude.mkdir()
    host_cred = host_claude / ".credentials.json"
    host_cred.write_text("{}")
    state_dir = tmp_path / "state"
    # Act
    resolved = resolve_cred_file(
        _config(tmp_path / "wd", account=""),
        state_dir,
        now=time.time(),
    )
    # Assert
    assert resolved == host_cred
