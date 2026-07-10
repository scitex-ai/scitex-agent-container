"""Tests for the reactive rate-limit supervisor wiring (task #13).

``handle_rate_limit_failure`` is the glue between the pure detector
(``_account.rate_limit_signals.detect_signal_from_text``), the pure
classifier (``_account.rate_limit_classifier.classify_rate_limit_signal``),
and the two action-layer functions (``_account.backoff_agent.backoff_agent``
/ ``_account.rotate_account.rotate_account``). These tests exercise it
end-to-end with REAL capturing callables (no mock libs) and, for the
Mode B (rotate) cases, a REAL isolated ``tmp_path``-rooted account
store — never the process's actual ``$HOME``.

Deterministic-by-signal-choice: HTTP_529 is ALWAYS Mode A (BACKOFF)
and TEXTUAL_MATCH is ALWAYS Mode B (ROTATE) regardless of usage%
(see ``rate_limit_classifier``), so these tests don't need to control
the cached usage-%% snapshot to get a deterministic Mode — the 429/403
usage-threshold boundary itself is already fully covered by
``test_rate_limit_classifier.py``.

Test style (STX-TQ002 / STX-TQ007): explicit ``# Arrange`` / ``# Act``
/ ``# Assert`` markers each on their own line, in order; one logical
assertion per test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scitex_agent_container._account.backoff_agent import DEFAULT_MIN_BACKOFF_S
from scitex_agent_container._runners._rate_limit_reactive import (
    RATE_LIMITED_CAUSE,
    ReactiveOutcome,
    handle_rate_limit_failure,
)

# ---------------------------------------------------------------------------
# Capturing helpers (no mock libs — real callables recording real calls)
# ---------------------------------------------------------------------------


def _make_append_session_message(events: list):
    def _append(state_dir, payload):
        events.append(payload)

    return _append


def _make_report_sdk_error(calls: list):
    def _report(*, name, host, cause, detail=None, turn_id=None, db_writer=None):
        calls.append({"name": name, "host": host, "cause": cause, "detail": detail})
        return len(calls)

    return _report


def _make_home(tmp_path: Path, email: str = "primary@example.com") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )
    return home


def _make_store(tmp_path: Path, accounts: list[dict]) -> Path:
    future_ms = int((time.time() + 30 * 24 * 3600) * 1000)
    store = tmp_path / "store"
    store.mkdir()
    for raw in accounts:
        acct = dict(raw)
        name = acct["name"]
        cred_dir = store / name
        cred_dir.mkdir()
        (cred_dir / "account.json").write_text(json.dumps(acct))
        (cred_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"expiresAt": future_ms}})
        )
    return store


def _call(
    tmp_path,
    *,
    enriched: str,
    consecutive_hits: int = 0,
    attempt: int = 0,
    home: Path | None = None,
    store_dir: Path | None = None,
):
    events: list[dict] = []
    db_calls: list[dict] = []
    outcome = handle_rate_limit_failure(
        enriched=enriched,
        state_dir=tmp_path / "state",
        name="agent-x",
        host="testhost",
        attempt=attempt,
        consecutive_hits=consecutive_hits,
        stderr_event_fields={},
        append_session_message=_make_append_session_message(events),
        report_sdk_error=_make_report_sdk_error(db_calls),
        db_writer=None,
        account_home=home,
        account_store_dir=store_dir,
    )
    return outcome, events, db_calls


# ---------------------------------------------------------------------------
# No signal — pure no-op, falls through unchanged
# ---------------------------------------------------------------------------


def test_no_signal_is_not_handled(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(tmp_path, enriched="connection reset by peer")
    # Assert
    assert outcome.handled is False


def test_no_signal_emits_no_session_events(tmp_path):
    # Arrange / Act
    _outcome, events, _db = _call(tmp_path, enriched="connection reset by peer")
    # Assert
    assert events == []


def test_no_signal_reports_no_db_errors(tmp_path):
    # Arrange / Act
    _outcome, _events, db_calls = _call(tmp_path, enriched="connection reset by peer")
    # Assert
    assert db_calls == []


def test_no_signal_returns_reactive_outcome_instance(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(tmp_path, enriched="connection reset by peer")
    # Assert
    assert isinstance(outcome, ReactiveOutcome)


# ---------------------------------------------------------------------------
# HTTP_529 — always Mode A (BACKOFF), same account
# ---------------------------------------------------------------------------


def test_529_is_handled(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert outcome.handled is True


def test_529_does_not_reset_attempt(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert outcome.reset_attempt is False


def test_529_delay_is_at_least_the_backoff_floor(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert outcome.delay_s >= DEFAULT_MIN_BACKOFF_S


def test_529_emits_account_backoff_supervisor_event(tmp_path):
    # Arrange / Act
    _outcome, events, _db = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert any(e.get("event") == "account_backoff" for e in events)


def test_529_emits_error_kind_event(tmp_path):
    # Arrange / Act
    _outcome, events, _db = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert any(e.get("kind") == "rate_limited" for e in events)


def test_529_reports_rate_limited_cause_to_db(tmp_path):
    # Arrange / Act
    _outcome, _events, db_calls = _call(
        tmp_path, enriched='{"error":{"type":"overloaded_error"}}'
    )
    # Assert
    assert any(c["cause"] == RATE_LIMITED_CAUSE for c in db_calls)


def test_529_consecutive_hits_increments_from_prior(tmp_path):
    # Arrange / Act
    outcome, _events, _db = _call(
        tmp_path,
        enriched='{"error":{"type":"overloaded_error"}}',
        consecutive_hits=2,
    )
    # Assert
    assert outcome.consecutive_hits == 3


def test_529_escalates_to_rotate_after_five_consecutive_hits(tmp_path):
    # Arrange — a healthy backup account IS available, and this is the
    # 5th consecutive Mode-A hit -> the action layer should escalate to
    # Mode B even though the classifier itself said BACKOFF.
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    outcome, _events, _db = _call(
        tmp_path,
        enriched='{"error":{"type":"overloaded_error"}}',
        consecutive_hits=4,
        home=home,
        store_dir=store,
    )
    # Assert
    assert outcome.reset_attempt is True


# ---------------------------------------------------------------------------
# TEXTUAL_MATCH — always Mode B (ROTATE)
# ---------------------------------------------------------------------------

_WEEKLY_LIMIT_TEXT = "You've hit your weekly limit · resets 2026-06-18T05:00Z"


def test_textual_match_with_healthy_account_rotates(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    outcome, _events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert outcome.reset_attempt is True


def test_textual_match_rotation_zeroes_consecutive_hits(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    outcome, _events, _db = _call(
        tmp_path,
        enriched=_WEEKLY_LIMIT_TEXT,
        consecutive_hits=3,
        home=home,
        store_dir=store,
    )
    # Assert
    assert outcome.consecutive_hits == 0


def test_textual_match_emits_account_rotated_event(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    _outcome, events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert any(e.get("event") == "account_rotated" for e in events)


def test_textual_match_actually_switches_the_credential_file(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    _call(tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store)
    # Assert — the real effect: switch_account copied the backup snapshot in.
    assert (home / ".claude" / ".credentials.json").is_file()


def test_textual_match_no_healthy_account_falls_back_to_backoff(tmp_path):
    # Arrange — "never park": no candidate to rotate to, must still retry.
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    outcome, _events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert outcome.reset_attempt is False


def test_textual_match_no_healthy_account_still_handled(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    outcome, _events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert outcome.handled is True


def test_textual_match_no_healthy_account_emits_rotate_skipped_event(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    _outcome, events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert any(e.get("event") == "account_rotate_skipped" for e in events)


def test_textual_match_no_healthy_account_also_emits_backoff_event(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    _outcome, events, _db = _call(
        tmp_path, enriched=_WEEKLY_LIMIT_TEXT, home=home, store_dir=store
    )
    # Assert
    assert any(e.get("event") == "account_backoff" for e in events)
