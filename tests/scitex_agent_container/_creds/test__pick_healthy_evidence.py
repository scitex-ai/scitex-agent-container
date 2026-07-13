"""Self-diagnosing-evidence tests for ``_creds._pick_healthy``.

INCIDENT 2026-07-10 (``sac agents start --group infra`` total boot
failure): the picker's "EXPIRED (-5.8h)" error hid WHICH file and WHICH
raw ``expiresAt`` it evaluated. When the snapshot was rewritten by a
login-capture minutes after the probe, the terse message made a correct
read look like a false one and cost the investigation hours. These
tests lock the evidence contract: every health record and every
``NoHealthyAccountError`` names the file, the literal ``expiresAt``
value, and the probe time — and a snapshot with a FUTURE expiry can
never be reported expired.

No mocks (PA-306): real JSON snapshots under a tmp store. AAA markers
(TQ002); one assertion per test (TQ007). Same ``_isolate_home`` idiom as
the sibling ``test__pick_healthy``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._creds._pick_healthy import (
    NoHealthyAccountError,
    account_health,
    pick_healthy_account,
)


@pytest.fixture
def _isolate_home(tmp_path: Path):
    """Force ``Path.home()`` inside ``tmp_path`` for the test's duration."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture(autouse=True)
def _isolate_quota_cache(tmp_path: Path):
    """Point the quota-cache reader at a nonexistent tmp file.

    Agent containers bind the LIVE fleet ``/var/sac/quota-cache.json``
    (the reader's default path); an explicitly-absent override keeps
    these evidence tests independent of real production utilisation.
    """
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(tmp_path / "absent-quota-cache.json")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _snapshot_path(home: Path, name: str) -> Path:
    return (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )


def _write_snapshot(home: Path, name: str, expires_at_ms: int) -> Path:
    """Write a full-shape credential snapshot (incident-real fields)."""
    path = _snapshot_path(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-not-real",
                    "refreshToken": "sk-ant-ort-not-real",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# Regression: a FUTURE expiresAt must never read as EXPIRED.
# ---------------------------------------------------------------------------


def test_future_expiry_snapshot_is_never_reported_expired(
    _isolate_home: Path,
) -> None:
    # Arrange — the incident's healthy-account shape: a fresh ms-epoch
    # expiry ~8h out (what the repaired wyusuuke snapshot held).
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", int((time.time() + 8 * 3600) * 1000))
    # Act
    health = account_health("wyusuuke-gmail-com", home=home)
    # Assert
    assert health.state == "VALID"


def test_future_expiry_snapshot_is_picked_not_refused(_isolate_home: Path) -> None:
    # Arrange — one healthy candidate must always boot (no false
    # NoHealthyAccountError on a future expiry).
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", int((time.time() + 8 * 3600) * 1000))
    # Act
    picked = pick_healthy_account(
        "wyusuuke-gmail-com", candidates=["wyusuuke-gmail-com"], home=home
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


# ---------------------------------------------------------------------------
# Evidence on the health record itself.
# ---------------------------------------------------------------------------


def test_account_health_records_the_snapshot_file_it_read(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    written = _write_snapshot(home, "acct-a", int((time.time() + 3600) * 1000))
    # Act
    health = account_health("acct-a", home=home)
    # Assert
    assert health.snapshot_path == str(written)


def test_account_health_records_the_raw_expires_at_value(
    _isolate_home: Path,
) -> None:
    # Arrange — the incident's literal pre-repair value.
    home = _isolate_home
    _write_snapshot(home, "acct-a", 1783672205000)
    # Act
    health = account_health("acct-a", home=home)
    # Assert
    assert health.expires_at_raw == 1783672205000.0


def test_absent_account_health_still_names_the_probed_path(
    _isolate_home: Path,
) -> None:
    # Arrange — no snapshot written; the record must still say WHERE it
    # looked so a wrong-store-resolution bug is visible from the record.
    home = _isolate_home
    # Act
    health = account_health("ghost-account", home=home)
    # Assert
    assert health.snapshot_path == str(_snapshot_path(home, "ghost-account"))


# ---------------------------------------------------------------------------
# Evidence in the NoHealthyAccountError message.
# ---------------------------------------------------------------------------


def _pick_error_message(
    home: Path, candidate: str, now: float | None = None
) -> str:
    """Run the picker expecting NoHealthyAccountError; return its message.

    Returns ``""`` when no error was raised so a content assertion fails
    loudly instead of passing vacuously. Keeps each test at exactly ONE
    assertion (TQ007 counts ``pytest.raises`` blocks as assertions).
    """
    try:
        pick_healthy_account(candidate, candidates=[candidate], home=home, now=now)
    except NoHealthyAccountError as exc:
        return str(exc)
    return ""


def test_no_healthy_error_names_the_snapshot_file_path(_isolate_home: Path) -> None:
    # Arrange — one expired candidate (the incident shape).
    home = _isolate_home
    written = _write_snapshot(home, "acct-a", 1783672205000)
    # Act
    message = _pick_error_message(home, "acct-a")
    # Assert
    assert str(written) in message


def test_no_healthy_error_quotes_the_raw_expires_at_value(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "acct-a", 1783672205000)
    # Act
    message = _pick_error_message(home, "acct-a")
    # Assert
    assert "expiresAt=1783672205000" in message


def test_no_healthy_error_renders_the_expiry_as_utc(_isolate_home: Path) -> None:
    # Arrange — 1783672205000 ms = 2026-07-10T08:30:05+00:00 (the exact
    # pre-repair wyusuuke expiry behind the incident's "-5.8h").
    home = _isolate_home
    _write_snapshot(home, "acct-a", 1783672205000)
    # Act
    message = _pick_error_message(home, "acct-a")
    # Assert
    assert "2026-07-10T08:30:05+00:00" in message


def test_no_healthy_error_pins_the_probe_timestamp(_isolate_home: Path) -> None:
    # Arrange — the probe time makes a probe-vs-recheck race
    # adjudicable from the error line alone.
    home = _isolate_home
    _write_snapshot(home, "acct-a", 1783672205000)
    # Act
    message = _pick_error_message(home, "acct-a", now=1783693260.0)
    # Assert
    assert "probed at 2026-07-10T14:21:00+00:00" in message


def test_no_healthy_error_marks_absent_snapshot_with_its_path(
    _isolate_home: Path,
) -> None:
    # Arrange — ABSENT candidate: the error must name the path that was
    # probed and found missing/unparseable.
    home = _isolate_home
    # Act
    message = _pick_error_message(home, "ghost")
    # Assert
    assert str(_snapshot_path(home, "ghost")) in message
