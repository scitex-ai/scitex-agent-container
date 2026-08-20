"""Tests for the ACL-deny rate-limit log (sac-comms item D).

Lead a2a ``c42b3e3c`` (merged with
``lead-sac-acl-blocked-attempt-notification``): when an outbound
``a2a_send`` is ACL-denied, a synthetic system notification is
published at the TARGET; the publish is rate-limited per
(sender, target) pair via the ``acl_deny_notify_log`` table.

This module covers the rate-limit primitive in isolation; the
:mod:`_listen._node_channel` integration (deny branch actually
publishes the synthetic frame at most once per cool-down window)
has its own end-to-end test file.

No-mocks (PA-306): real on-disk state.db, env + module constant
save/restore. AAA markers (TQ002), one assert per test (TQ007),
3+-word test names.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_acl_deny_notify import (
    DEFAULT_COOLDOWN_S,
    last_notified_at,
    resolve_cooldown_s,
    should_notify_acl_deny,
)


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    saved_cooldown_env = os.environ.get("SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env
        if saved_cooldown_env is None:
            os.environ.pop("SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S", None)
        else:
            os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = saved_cooldown_env


# ---------------------------------------------------------------------------
# should_notify_acl_deny — first call admits, second call inside cool-down
# suppresses; after cool-down elapses, admits again.
# ---------------------------------------------------------------------------


def test_first_call_returns_true(pg_schema: str) -> None:
    # Arrange — fresh DB, no prior row for the pair.
    # Act
    admitted = should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    # Assert — first denied attempt MUST emit the synthetic notification.
    assert admitted is True


def test_second_call_within_cooldown_returns_false(
    pg_schema: str,
) -> None:
    # Arrange — record an admit at t=1000, cool-down=60s.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    # Act — second attempt only 30s later (inside the window).
    admitted = should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1030.0,
    )
    # Assert — duplicate inside cool-down MUST suppress (no flood).
    assert admitted is False


def test_call_after_cooldown_elapses_returns_true(
    pg_schema: str,
) -> None:
    # Arrange — cool-down=60s; second attempt 61s later (just past).
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    # Act
    admitted = should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1061.0,
    )
    # Assert — past the window, re-emit so the operator who missed
    # the first frame sees a follow-up.
    assert admitted is True


def test_different_pair_not_throttled_by_unrelated(
    pg_schema: str,
) -> None:
    # Arrange — throttle is per (sender, target). One pair's emit
    # MUST NOT silence a different pair.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    # Act — different sender, same target.
    admitted = should_notify_acl_deny(
        sender="bob",
        target="lead",
        cooldown_s=60.0,
        now=1005.0,
    )
    # Assert
    assert admitted is True


def test_same_sender_different_target_admitted(
    pg_schema: str,
) -> None:
    # Arrange — same sender, different target — independent key.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    # Act
    admitted = should_notify_acl_deny(
        sender="alice",
        target="other",
        cooldown_s=60.0,
        now=1005.0,
    )
    # Assert
    assert admitted is True


def test_admit_updates_last_notified_at(pg_schema: str) -> None:
    # Arrange — capture the stamp the admit recorded.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1234.5,
    )
    # Act
    stamp = last_notified_at(sender="alice", target="lead")
    # Assert — observability surface MUST round-trip the recorded ts.
    assert stamp == 1234.5


def test_re_admit_overwrites_last_notified_at(pg_schema: str) -> None:
    # Arrange — first admit at t=1000, second (past cool-down) at t=2000.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=2000.0,
    )
    # Act
    stamp = last_notified_at(sender="alice", target="lead")
    # Assert — the second admit MUST bump the stamp forward so the
    # next cool-down is measured from the most recent emit.
    assert stamp == 2000.0


def test_suppressed_call_does_not_bump_last_notified_at(
    pg_schema: str,
) -> None:
    # Arrange — a suppressed (inside-window) attempt MUST NOT slide
    # the cool-down forward. Otherwise a sender attempting every
    # second would extend the silence indefinitely.
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1000.0,
    )
    should_notify_acl_deny(
        sender="alice",
        target="lead",
        cooldown_s=60.0,
        now=1030.0,
    )
    # Act
    stamp = last_notified_at(sender="alice", target="lead")
    # Assert
    assert stamp == 1000.0


# ---------------------------------------------------------------------------
# Fail-loud — empty sender / target rejected
# ---------------------------------------------------------------------------


def test_empty_sender_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        should_notify_acl_deny(
            sender="",
            target="lead",
            cooldown_s=60.0,
        )


def test_empty_target_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        should_notify_acl_deny(
            sender="alice",
            target="",
            cooldown_s=60.0,
        )


def test_last_notified_at_absent_pair_returns_none(
    pg_schema: str,
) -> None:
    # Arrange — no admit calls for this pair.
    # Act
    stamp = last_notified_at(sender="ghost", target="lead")
    # Assert
    assert stamp is None


# ---------------------------------------------------------------------------
# resolve_cooldown_s — env / default / override precedence
# ---------------------------------------------------------------------------


def test_default_cooldown_is_thirty_minutes(pg_schema: str) -> None:
    # Arrange — no env, no override.
    os.environ.pop("SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S", None)
    # Act
    effective = resolve_cooldown_s()
    # Assert — 30 min per lead's directive.
    assert effective == DEFAULT_COOLDOWN_S


def test_env_overrides_default(pg_schema: str) -> None:
    # Arrange — operator sets a custom window.
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "5"
    # Act
    effective = resolve_cooldown_s()
    # Assert
    assert effective == 5.0


def test_explicit_override_beats_env(pg_schema: str) -> None:
    # Arrange — test seam: explicit arg wins over env wins over default.
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "999"
    # Act
    effective = resolve_cooldown_s(0.5)
    # Assert
    assert effective == 0.5


def test_malformed_env_falls_back_to_default(pg_schema: str) -> None:
    # Arrange — operator typo (non-float) MUST NOT silently disable
    # the rate-limit; fall back to the safe default.
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "not-a-number"
    # Act
    effective = resolve_cooldown_s()
    # Assert
    assert effective == DEFAULT_COOLDOWN_S


def test_negative_env_falls_back_to_default(pg_schema: str) -> None:
    # Arrange — "-1" would disable the rate-limit (every attempt
    # past, since elapsed >= -1 always). Safe fallback per spec.
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "-1"
    # Act
    effective = resolve_cooldown_s()
    # Assert
    assert effective == DEFAULT_COOLDOWN_S
