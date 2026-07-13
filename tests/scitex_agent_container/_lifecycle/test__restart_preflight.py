"""Unit tests for the PRE-STOP successor-credential auth pre-flight.

INCIDENT ``incident-agent-self-restart-one-way-20260712``: a brokered
self-restart stopped the agent then re-launched a container that boots
DEAD ("Login expired") because the timestamp-only launch gate admits a
snapshot whose ``expiresAt`` is in the future but whose refresh grant is
server-invalidated. :mod:`scitex_agent_container._lifecycle.
_restart_preflight` probes the SUCCESSOR credential's REAL usability
BEFORE the stop and ABORTS (container LEFT UP) on a rejected grant.

No mocks: real ``AgentConfig`` dataclasses, real on-disk snapshots under
an isolated ``$HOME``, and a real callable injected at
``refresh_account_credentials``'s documented ``opener`` urllib seam. AAA
markers, descriptive names, one assertion each. Token values NEVER
appear in fixtures, messages, or assertions.

The ``agent_restart`` / ``agent_start`` seam integration lives in
``test__restart_preflight_integration.py`` (kept separate for the
per-file line budget).
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from scitex_agent_container._lifecycle._restart_preflight import (
    RestartPreflightAbort,
    assert_successor_auth_usable,
    resolve_successor_credential,
)
from scitex_agent_container.config import AgentConfig

# Non-secret fixture sentinels for the OAuth block. Deliberately NOT
# secret-shaped (no ``sk-ant`` prefix, low entropy) so they are obviously
# test data and never redacted / mistaken for a real token.
_RT = "rt-fixture-value"
_AT = "at-fixture-value"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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


def _snapshot_path(home: Path, name: str) -> Path:
    return (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )


def _write_snapshot(
    home: Path, name: str, expires_ms: int, *, refresh: str | None = _RT
) -> Path:
    path = _snapshot_path(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, Any] = {"expiresAt": expires_ms, "accessToken": _AT}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return path


def _future_ms(seconds: float = 7200.0) -> int:
    return int((time.time() + seconds) * 1_000)


def _account_config(name: str, account: str) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.account = account
    return cfg


# --- real urllib ``opener`` seams (no mocks) -------------------------------


class _Resp:
    """Real context-manager response with the urlopen ``.read()`` shape."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


def _ok_opener(expires_in: int = 3600) -> Callable[..., _Resp]:
    """200 with a fresh access_token — the refresh SUCCEEDS (heals)."""

    def _open(req: Any, timeout: int = 15) -> _Resp:
        return _Resp(
            json.dumps(
                {
                    "access_token": _AT + "-rotated",
                    "refresh_token": _RT + "-rotated",
                    "expires_in": expires_in,
                }
            ).encode()
        )

    return _open


def _reject_opener() -> Callable[..., _Resp]:
    """HTTP 400 invalid_grant — the endpoint EVALUATED and REFUSED the grant."""

    def _open(req: Any, timeout: int = 15) -> _Resp:
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"invalid_grant"}'),
        )

    return _open


def _moved_opener() -> Callable[..., _Resp]:
    """HTTP 404 — endpoint moved (2026-07-10 class): TRANSPORT, not a token."""

    def _open(req: Any, timeout: int = 15) -> _Resp:
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"not_found"}')
        )

    return _open


def _network_opener() -> Callable[..., _Resp]:
    """Raw network failure — TRANSPORT, never a token rejection."""

    def _open(req: Any, timeout: int = 15) -> _Resp:
        raise urllib.error.URLError("connection refused")

    return _open


# ---------------------------------------------------------------------------
# resolve_successor_credential — scoping
# ---------------------------------------------------------------------------


def test_unpinned_config_resolves_to_no_credential(_isolate_home: Path) -> None:
    # Arrange — no account / credentials_file: host-live, never swaps.
    cfg = AgentConfig(name="alpha")
    # Act
    path, label = resolve_successor_credential(cfg)
    # Assert — pre-flight is a no-op for pure-unpinned agents.
    assert path is None


def test_account_config_resolves_to_its_snapshot(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    snap = _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act
    path, label = resolve_successor_credential(cfg)
    # Assert
    assert path == snap and label == "wyusuuke-gmail-com"


def test_credentials_file_config_resolves_to_parent_slug(_isolate_home: Path) -> None:
    # Arrange — a collapsed pool sets spec.claude.credentials_file.
    home = _isolate_home
    snap = _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    cfg = AgentConfig(name="alpha")
    cfg.claude.credentials_file = str(snap)
    # Act
    path, label = resolve_successor_credential(cfg)
    # Assert — label is the account slug (parent dir), per the fleet layout.
    assert label == "ywata1989-gmail-com"


# ---------------------------------------------------------------------------
# assert_successor_auth_usable — the abort decision
# ---------------------------------------------------------------------------


def test_unpinned_config_is_a_noop_even_with_a_rejecting_opener(
    _isolate_home: Path,
) -> None:
    # Arrange — an unpinned agent must NEVER be probed (nothing to probe).
    cfg = AgentConfig(name="alpha")
    # Act
    result = assert_successor_auth_usable(cfg, opener=_reject_opener())
    # Assert — returns cleanly (no probe, no raise).
    assert result is None


def test_rejected_grant_raises_restart_preflight_abort(_isolate_home: Path) -> None:
    # Arrange — snapshot unexpired by TIMESTAMP, but the refresh is rejected.
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act
    ctx = pytest.raises(RestartPreflightAbort)
    # Assert — the confirmed one-way-restart incident class.
    with ctx:
        assert_successor_auth_usable(cfg, opener=_reject_opener())


def test_abort_message_names_the_account_and_the_refresh_remedy(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act — actionable: names the account + the exact remedy command.
    ctx = pytest.raises(
        RestartPreflightAbort, match=r"wyusuuke-gmail-com.*sac accounts refresh"
    )
    # Assert
    with ctx:
        assert_successor_auth_usable(cfg, opener=_reject_opener())


def test_abort_message_confirms_the_container_is_left_up(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act — the operator must know the running agent was NOT torn down.
    ctx = pytest.raises(RestartPreflightAbort, match="LEFT UP")
    # Assert
    with ctx:
        assert_successor_auth_usable(cfg, opener=_reject_opener())


def test_abort_message_contains_no_token_value(_isolate_home: Path) -> None:
    # Arrange — a distinctive refresh_token that must NEVER leak into the error.
    home = _isolate_home
    _write_snapshot(
        home, "wyusuuke-gmail-com", _future_ms(), refresh="rt-MUST-NOT-APPEAR"
    )
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    captured = ""
    # Act
    try:
        assert_successor_auth_usable(cfg, opener=_reject_opener())
    except RestartPreflightAbort as exc:
        captured = str(exc)
    # Assert — value-safety: no token material in the surfaced message.
    assert "rt-MUST-NOT-APPEAR" not in captured


def test_successful_refresh_does_not_raise(_isolate_home: Path) -> None:
    # Arrange — the refresh chain WORKS; the successor is safe to launch.
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act
    result = assert_successor_auth_usable(cfg, opener=_ok_opener())
    # Assert — a healthy successor must never block the restart.
    assert result is None


def test_successful_refresh_heals_the_snapshot_expiry(_isolate_home: Path) -> None:
    # Arrange — a near-expiry snapshot whose refresh still works.
    home = _isolate_home
    snap = _write_snapshot(home, "wyusuuke-gmail-com", _future_ms(120))
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    before = json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"]
    # Act — the probe-refresh atomically persists a fresh token block.
    assert_successor_auth_usable(cfg, opener=_ok_opener(expires_in=3600))
    # Assert — the successor now binds an already-warmed credential.
    after = json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"]
    assert after > before


def test_moved_endpoint_fails_open(_isolate_home: Path) -> None:
    # Arrange — HTTP 404 (endpoint moved) is TRANSPORT, not a dead token.
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act — MUST NOT raise: blocking a healthy restart is worse than the bug.
    result = assert_successor_auth_usable(cfg, opener=_moved_opener())
    # Assert
    assert result is None


def test_network_error_fails_open(_isolate_home: Path) -> None:
    # Arrange — a raw network failure must never block a restart.
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act
    result = assert_successor_auth_usable(cfg, opener=_network_opener())
    # Assert
    assert result is None


def test_missing_refresh_token_fails_open(_isolate_home: Path) -> None:
    # Arrange — no refresh_token: unrefreshable, but this is NOT the
    # proven-rejected class, so fail OPEN (never block a restart).
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms(), refresh=None)
    cfg = _account_config("alpha", "wyusuuke-gmail-com")
    # Act — no refresh_token → returns before any POST (opener never fires).
    result = assert_successor_auth_usable(cfg, opener=_reject_opener())
    # Assert
    assert result is None


def test_absent_account_snapshot_raises_before_any_probe(_isolate_home: Path) -> None:
    # Arrange — pinned account with NO snapshot on disk.
    from scitex_agent_container.runtimes._apptainer_creds import PinnedAccountError

    cfg = _account_config("alpha", "ghost-account")
    # Act — resolution fails loud (abort-before-stop), never reaches the probe.
    ctx = pytest.raises(PinnedAccountError)
    # Assert
    with ctx:
        assert_successor_auth_usable(cfg, opener=_ok_opener())
