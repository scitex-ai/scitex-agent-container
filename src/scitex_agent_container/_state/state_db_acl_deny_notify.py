"""ACL-deny rate-limited synthetic-notification log (sac-comms item D).

Per lead a2a ``c42b3e3c`` (2026-06-13) — merged with
``lead-sac-acl-blocked-attempt-notification``:

  * When an outbound ``a2a_send(sender, target)`` is ACL-denied (403),
    the TARGET (receiver) gets a *synthetic* system-level inbox
    notification: "Sender X attempted a send to you and was blocked
    by ACL; grant via `sac a2a grant X <you>` if intended."
  * The notification *bypasses* ACL — it is published directly onto
    the receiver's inbox channel so the operator can grant
    proactively instead of reactively.
  * Rate-limited per ``(sender, target)`` pair: at most one
    notification per cool-down window (default 30 min, env-overridable
    via ``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S``).
  * This REPLACES the prior parent/child auto-grant policy. The
    rate-limited notification is the substitute for "auto-grant if
    intra-lineage": the operator sees the attempt once per cooldown
    window and decides.

This module owns the rate-limit log only — the actual notification
mint + publish lives in ``_listen._node_channel`` (which calls
:func:`should_notify_acl_deny` to decide whether to push or
suppress the redundant frame).

Schema is minimal: ``(sender, target, last_notified_at)`` with
``(sender, target)`` PRIMARY KEY. The check-and-update is atomic
within a single transaction so a concurrent burst of denied
attempts publishes at most one notification per cooldown window.

No-mocks (PA-306) testing — real on-disk sqlite, no monkeypatch.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = [
    "DEFAULT_COOLDOWN_S",
    "ensure_acl_deny_notify_log_table",
    "last_notified_at",
    "resolve_cooldown_s",
    "should_notify_acl_deny",
]


# 30 minutes — sensible default per lead's directive. Long enough to
# avoid receiver-side flood from a misbehaving sender; short enough
# that an operator who missed the first frame still sees a follow-up
# within their attention window.
DEFAULT_COOLDOWN_S: float = 30.0 * 60.0

# Env knob name (one of the audit-compliant top-level
# ``SCITEX_AGENT_CONTAINER_*``-shape vars would be more idiomatic
# but the operator-facing knob convention for runtime tuning is
# ``SCITEX_*`` — and this is a per-process runtime knob, not a
# packaging-time toggle).
_COOLDOWN_ENV = "SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS acl_deny_notify_log (
    sender            TEXT NOT NULL,
    target            TEXT NOT NULL,
    last_notified_at  REAL NOT NULL,
    PRIMARY KEY (sender, target)
);
"""


def ensure_acl_deny_notify_log_table(db_path: Path | None = None) -> None:
    """Idempotent CREATE TABLE for the deny-notify rate-limit log.

    Called from :func:`_state.state_db.init_schema` so a fresh
    state.db carries the table; safe to call multiple times.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)


def resolve_cooldown_s(override: float | None = None) -> float:
    """Return the effective cool-down window in seconds.

    Resolution order:

      1. Explicit ``override`` argument (test seam — pass a tiny
         value so a unit test can exercise the elapse case without a
         30-minute wait).
      2. Env var ``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S`` (operator
         runtime knob — accepts a float number of seconds).
      3. :data:`DEFAULT_COOLDOWN_S` (30 minutes).

    A negative / non-numeric env value falls back to the default
    (fail-loud at the parse boundary — silently honouring "-1" would
    disable the rate-limit and surprise the operator).
    """
    if override is not None:
        return float(override)
    raw = os.environ.get(_COOLDOWN_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_COOLDOWN_S
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_COOLDOWN_S
    if parsed < 0:
        return DEFAULT_COOLDOWN_S
    return parsed


def should_notify_acl_deny(
    *,
    sender: str,
    target: str,
    cooldown_s: float | None = None,
    db_path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Atomically decide whether to publish a deny-notification frame.

    Returns ``True`` iff the caller should emit a synthetic
    notification to ``target``. On ``True`` the row is upserted
    with the supplied ``now`` timestamp, so a concurrent burst of
    denied attempts publishes at most one notification per cool-down
    window.

    Returns ``False`` when a prior notification was emitted within
    the cool-down window (suppress to avoid receiver flood).

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError`` —
    a deny with no identity has no per-pair key to throttle on.

    ``cooldown_s`` overrides the env / default (test seam).
    ``now`` is the wall clock to record (test seam); defaults to
    :func:`time.time`.
    """
    if not sender or not target:
        raise ValueError("should_notify_acl_deny: sender and target must be non-empty")
    effective_cooldown = resolve_cooldown_s(cooldown_s)
    effective_now = time.time() if now is None else float(now)
    ensure_acl_deny_notify_log_table(db_path)
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_notified_at FROM acl_deny_notify_log "
            "WHERE sender = ? AND target = ?",
            (sender, target),
        ).fetchone()
        if row is not None:
            elapsed = effective_now - float(row["last_notified_at"])
            if elapsed < effective_cooldown:
                return False
        # First time, or cool-down elapsed — upsert + return True.
        conn.execute(
            "INSERT INTO acl_deny_notify_log (sender, target, last_notified_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(sender, target) DO UPDATE SET "
            "  last_notified_at = excluded.last_notified_at",
            (sender, target, effective_now),
        )
    return True


def last_notified_at(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> float | None:
    """Return the timestamp of the most recent deny-notify for the pair.

    Returns ``None`` when no notification has ever been published for
    the pair. Observability / test surface — the rate-limit decision
    itself goes through :func:`should_notify_acl_deny` so the
    check+update is one transaction.
    """
    if not sender or not target:
        return None
    ensure_acl_deny_notify_log_table(db_path)
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_notified_at FROM acl_deny_notify_log "
            "WHERE sender = ? AND target = ?",
            (sender, target),
        ).fetchone()
    if row is None:
        return None
    return float(row["last_notified_at"])
