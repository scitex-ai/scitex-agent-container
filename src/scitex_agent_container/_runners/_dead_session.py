"""Classify an SDK conversation failure that is a *dead session resume*.

When a long-lived ``claude-session`` runner tries to ``--resume`` a
session id the Claude server has already aged out (server-side TTL is
finite) or that never existed, the ``claude-agent-sdk`` does not surface
a typed exception. It bubbles up as a generic ``ProcessError`` /
``RuntimeError`` whose message contains "No conversation found with
session ID: <uuid>" (the real reason, captured from the claude
subprocess's stderr).

Left unclassified, that failure lands in the conversation supervisor's
catch-all (``cause="sdk-crash"``) and the supervisor *retries the same
dead id* by walking ``session_id_history`` — which on a genuinely dead
session contains only that dead id (and possibly its equally-dead
forks). The supervisor then exhausts ``max_restarts`` and the
conversation loop (with its Telegram poller) DIES while the heartbeat
thread survives — the agent looks alive but is mute. This is the
production crash-loop that left clew/neurovista silent for ~5h
(2026-05-24).

This module turns that into self-healing: :func:`is_dead_session_resume`
recognises the signature so the supervisor can discard the dead id from
both the latest marker and the history (see
:func:`._session_id.discard_dead_session`) and immediately start a FRESH
session (``resume=None``) — preserving resume for genuinely-valid
sessions and only resetting on a confirmed-dead one.

:func:`extract_dead_session_id` recovers the specific uuid named in the
error so only that id is purged (a newer valid id is left intact).

Pure stdlib, no SDK import — unit-testable against plain strings.
"""

from __future__ import annotations

import re

__all__ = [
    "DEAD_SESSION_CAUSE",
    "extract_dead_session_id",
    "is_dead_session_resume",
]

# Short ``cause`` identifier written to ``state.db.errors``. Distinct
# from the generic ``sdk-crash`` so the operator can see at a glance
# that the runner self-healed from a stale resume marker rather than
# hit a transient network/SDK fault.
DEAD_SESSION_CAUSE = "dead-session"

# Lower-cased substrings that mark a failure as a dead/expired resume
# target. Matched against the stringified, stderr-enriched exception.
# Kept narrow so an unrelated tool-result blob that merely mentions a
# session id doesn't get misclassified — every needle is a phrase the
# SDK / claude subprocess emits when the --resume target is gone.
_DEAD_SESSION_SIGNATURES = (
    "no conversation found with session id",
    "no conversation found for session",
    "no conversation found with session",
    "session not found",
    "no such session",
)

# Captures the uuid named in "No conversation found with session ID:
# <uuid>". Tolerant of the exact preposition / punctuation around the id.
_SESSION_ID_RE = re.compile(
    r"session\s*id[:\s]+([0-9a-fA-F][0-9a-fA-F-]{7,})",
    re.IGNORECASE,
)


def is_dead_session_resume(error: object) -> bool:
    """Return True if ``error`` is a dead/expired ``--resume`` rejection.

    Parameters
    ----------
    error
        The exception (or any object) raised by the SDK conversation,
        already enriched with the captured subprocess stderr. Stringified
        and scanned case-insensitively for the "no conversation found"
        signatures in :data:`_DEAD_SESSION_SIGNATURES`.
    """
    haystack = str(error).lower()
    return any(sig in haystack for sig in _DEAD_SESSION_SIGNATURES)


def extract_dead_session_id(error: object) -> str | None:
    """Return the dead session uuid named in ``error``, or None.

    Used so the supervisor purges *only* the confirmed-dead id from the
    history rather than wiping every recorded id. Returns None when the
    error doesn't name a parseable id (the caller then falls back to
    discarding whatever the latest resume marker was).
    """
    match = _SESSION_ID_RE.search(str(error))
    if match is None:
        return None
    return match.group(1)
