"""The fleet AUTH-EVENT log: one collected, machine-parseable auth timeline.

Operator, 2026-07-18: 「サーバーが落とすんだから、ログを取ればいいんじゃないですか？
普通にバグのためにも開発のためにもログは必要なんじゃないですか？」 — the server is
what drops us, so log it; logs are needed for debugging and for development.

This package OBSERVES ONLY. It contains no detector, no restarter and no
remediation of any kind: ``auth-heal.py`` and :mod:`.._authheal` own that, and
a second actor on this rail would be the double-supervisor class in a new
costume. :mod:`._log` is the append-only writer/reader; :mod:`._timeline` joins
those events with the credential rotations :mod:`.._account._rotation_audit`
already records, so the causal event and its consequences finally read as one
ordered story. Surfaced as ``sac auth-events``.
"""

from __future__ import annotations

from ._account_label import resolve_account_for_agent
from ._log import (
    AUTH_EVENT_FILENAME,
    AUTH_EVENT_LOG_ENV,
    AUTH_FAILURE_OBSERVED,
    KNOWN_EVENTS,
    RESTART_ATTEMPTED,
    RESTART_OUTCOME,
    TOKEN_ROTATED,
    AuthEvent,
    auth_event_log_path,
    log_auth_event,
    log_auth_failure_observed,
    log_restart_attempted,
    log_restart_outcome,
    log_token_rotated,
    read_auth_events,
    unresolved_attempts,
)
from ._timeline import (
    ROTATION_AUDIT_FILENAME,
    rotation_audit_path,
    rotation_events,
    unified_timeline,
)

__all__ = [
    "AUTH_EVENT_FILENAME",
    "AUTH_EVENT_LOG_ENV",
    "AUTH_FAILURE_OBSERVED",
    "KNOWN_EVENTS",
    "RESTART_ATTEMPTED",
    "RESTART_OUTCOME",
    "ROTATION_AUDIT_FILENAME",
    "TOKEN_ROTATED",
    "AuthEvent",
    "auth_event_log_path",
    "log_auth_event",
    "log_auth_failure_observed",
    "log_restart_attempted",
    "log_restart_outcome",
    "log_token_rotated",
    "read_auth_events",
    "resolve_account_for_agent",
    "rotation_audit_path",
    "rotation_events",
    "unified_timeline",
    "unresolved_attempts",
]
