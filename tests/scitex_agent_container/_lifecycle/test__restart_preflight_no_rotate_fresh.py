#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The preflight must NOT spend the single-use refresh grant on a FRESH token.

INCIDENT 2026-07-13 — the probe WAS the outage.

``probe_credential_usable`` verified a credential BY REFRESHING IT. The OAuth
``refresh_token`` is SINGLE-USE, so the "probe" CONSUMED it and minted +
persisted a new access_token. On a shared account that is a fleet-wide
mutation: every OTHER agent pinned to that account is left holding the
previous token, 401s, and Claude Code renders the 401 as the misleading
"Login expired · Please run /login" while nothing has actually expired.

It ran on EVERY ``sac agents restart`` (pre-stop) and in ``agent_start``'s
force branch, and it called ``refresh_account_credentials`` DIRECTLY —
bypassing the ``sac accounts refresh`` CLI's "skipped; token still fresh
(TTL >= 2h)" guard — so it refreshed unconditionally, even against a token
with hours of life left. Restarting agents to fix them therefore rotated the
token on each restart, killing the agents just restarted. The operator
observed exactly that: even a manual restart came back login-required.

These tests pin the fix: a FRESH token is never probed; a NEAR-EXPIRY or
undeterminable one still is.

NO MOCKS: real credential files on disk, and the module's REAL ``opener``
injection seam. The recorder is a genuine urllib-shaped opener — the probe's
only route to the token endpoint is through it, so "never called" is direct
evidence that no refresh was POSTed and no token was rotated.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scitex_agent_container._lifecycle._restart_preflight import (
    _PROBE_MIN_TTL_S,
    probe_credential_usable,
)


class _RecordingOpener:
    """A real opener that records calls, then refuses to serve.

    The probe's only route to the token endpoint is this seam. If it is never
    invoked, no refresh grant was POSTed and the single-use refresh_token was
    NOT consumed — exactly the property under test.
    """

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append(args)
        raise AssertionError(
            "probe_credential_usable attempted a REFRESH. On a fresh token "
            "this consumes the SINGLE-USE refresh_token and rotates the "
            "shared access token, revoking it for every other agent on the "
            "account (the 2026-07-13 'Login expired' fleet outage)."
        )


def _write_credential(path: Path, *, ttl_seconds: float) -> Path:
    """Write a REAL credential file whose token expires in ``ttl_seconds``."""
    expires_at_ms = int((time.time() + ttl_seconds) * 1000)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "test-access-token-not-a-real-secret",
                    "refreshToken": "test-refresh-token-not-a-real-secret",
                    "expiresAt": expires_at_ms,
                }
            }
        )
    )
    return path


def _probe_fresh(tmp_path: Path) -> tuple[tuple, _RecordingOpener]:
    """Probe a comfortably-fresh credential; return (result, opener)."""
    cred = _write_credential(
        tmp_path / ".credentials.json", ttl_seconds=_PROBE_MIN_TTL_S + 3600
    )
    opener = _RecordingOpener()
    return probe_credential_usable(cred, opener=opener), opener


def test_fresh_token_probe_never_attempts_a_refresh(tmp_path: Path) -> None:
    """THE load-bearing test: a fresh token must not be rotated to check it."""
    # Arrange
    cred = _write_credential(
        tmp_path / ".credentials.json", ttl_seconds=_PROBE_MIN_TTL_S + 3600
    )
    opener = _RecordingOpener()

    # Act
    probe_credential_usable(cred, opener=opener)

    # Assert
    assert opener.calls == [], "the probe rotated a FRESH token — the outage"


def test_fresh_token_probe_reports_usable(tmp_path: Path) -> None:
    """Skipping the probe must still report the credential as usable."""
    # Arrange / see _probe_fresh
    # Act
    (usable, _kind, _reason), _opener = _probe_fresh(tmp_path)

    # Assert
    assert usable is True


def test_fresh_token_probe_reports_skipped_kind(tmp_path: Path) -> None:
    """The skip must be VISIBLE, not silent — callers log the reason."""
    # Arrange / see _probe_fresh
    # Act
    (_usable, kind, _reason), _opener = _probe_fresh(tmp_path)

    # Assert
    assert kind == "skipped-token-fresh"


def test_fresh_token_probe_explains_why_it_skipped(tmp_path: Path) -> None:
    """The reason must name the single-use grant, so nobody re-adds the probe."""
    # Arrange / see _probe_fresh
    # Act
    (_usable, _kind, reason), _opener = _probe_fresh(tmp_path)

    # Assert
    assert "SINGLE-USE" in (reason or "")


def test_fresh_token_file_is_left_byte_identical(tmp_path: Path) -> None:
    """A rewrite IS a rotation — independent evidence, even if a future
    refresh path were to bypass the opener seam."""
    # Arrange
    cred = _write_credential(
        tmp_path / ".credentials.json", ttl_seconds=_PROBE_MIN_TTL_S + 7200
    )
    before = cred.read_bytes()

    # Act
    probe_credential_usable(cred, opener=_RecordingOpener())

    # Assert
    assert cred.read_bytes() == before, "probe REWROTE a fresh credential"


def test_near_expiry_token_is_still_probed(tmp_path: Path) -> None:
    """The guard must not disable the check entirely: below the threshold a
    refresh is due anyway (the host timer would do it), so the probe RUNS."""
    # Arrange
    cred = _write_credential(
        tmp_path / ".credentials.json", ttl_seconds=_PROBE_MIN_TTL_S - 600
    )
    opener = _RecordingOpener()

    # Act
    try:
        probe_credential_usable(cred, opener=opener)
    except AssertionError:
        pass  # the recorder raises on use — REACHING it is the point

    # Assert
    assert opener.calls, "a near-expiry token was NOT probed; guard too broad"


def test_unreadable_credential_does_not_take_the_fresh_shortcut(
    tmp_path: Path,
) -> None:
    """An undetermined TTL must NOT be treated as fresh.

    If we cannot PROVE the token is fresh, we must not silently skip the
    safety check. (We assert on the SHORT-CIRCUIT, not on the opener: an
    unparseable file fails at the READ, so the real refresh path never reaches
    the network seam at all — asserting `opener.calls` here would be asserting
    something the code cannot do.)
    """
    # Arrange
    cred = tmp_path / ".credentials.json"
    cred.write_text("{ this is not valid json")

    # Act
    _usable, kind, _reason = probe_credential_usable(
        cred, opener=_RecordingOpener()
    )

    # Assert
    assert kind != "skipped-token-fresh", "undetermined TTL was assumed fresh"
