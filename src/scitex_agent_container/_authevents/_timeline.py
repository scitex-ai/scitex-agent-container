"""ONE timeline: auth events joined with the rotations that caused them.

THE ROTATION EVENT WAS NEVER MISSING — IT WAS UNJOINED
    The obvious design here is a new "token rotated" emitter. It would be
    wrong. :mod:`.._account._rotation_audit` has been recording every
    credential rotation to ``<accounts-store>/rotation-audit.jsonl`` for weeks,
    with the account, the reason, the host, the pid and opaque FROM→TO token
    fingerprints. Checked against the 2026-07-18 incident, the last record
    before six agents died reads::

        {"timestamp_utc": "2026-07-18T10:31:28.316535+00:00",
         "event": "refresh", "from_account": "alpha-example-com", ...
         "reason": "single-use refresh_token rotated (headless access-token
                    refresh)", "pid": 830472}

    10:31:28 UTC is 19:31:28 JST — the deaths, to the minute. The causal fact
    was on disk the whole time. What did not exist was anything that put it in
    the same timeline as the restarts that followed, so nobody looked.

    Adding a second rotation writer would therefore fix nothing and cost
    something real: two files claiming to record the same event, drifting, with
    no rule for which is authoritative. Instead this module PROJECTS the
    existing audit into the shared shape at READ time. ``rotation-audit.jsonl``
    stays the single source of truth for rotations and keeps its security
    contract (fingerprints only, never token material); the timeline borrows
    from it and owns nothing.

WHAT A MERGED TIMELINE LETS YOU ASK
    "Six agents died at 19:30 — what rotated just before?" becomes one ordered
    read instead of an inference from screens and stuck-durations. That is the
    entire ask, and it needs no new detector, restarter or daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._log import TOKEN_ROTATED, AuthEvent, read_auth_events

__all__ = [
    "ROTATION_AUDIT_FILENAME",
    "rotation_audit_path",
    "rotation_events",
    "unified_timeline",
]

#: Mirrors ``_account._rotation_audit.AUDIT_FILENAME``. Imported lazily in
#: :func:`rotation_audit_path` so a reader never drags the account stack in.
ROTATION_AUDIT_FILENAME = "rotation-audit.jsonl"


def rotation_audit_path() -> Path:
    """Where the rotation audit lives. Resolved per call; never raises."""
    # stx-allow: fallback (reason: this is a diagnostic reader. If the account
    # store cascade cannot be resolved we still want a usable default rather
    # than an exception thrown into an incident investigation.)
    try:
        from .._state.account_store import _store_path

        return _store_path(None, Path.home()) / ROTATION_AUDIT_FILENAME
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return (
            Path.home()
            / ".scitex"
            / "agent-container"
            / "accounts"
            / ROTATION_AUDIT_FILENAME
        )


def _project(record: dict[str, Any]) -> AuthEvent:
    """Turn ONE rotation-audit record into a :class:`AuthEvent`.

    The audit's ``to_account`` is the account that ends up holding the new
    token, so it is the account the event is ABOUT. Its ``event`` value
    (``refresh`` / ``switch`` / ``auto-rotate`` / …) is preserved verbatim
    under ``rotation_event`` — flattening those distinct triggers into one
    word would discard exactly the detail that says whether a rotation was a
    scheduled refresh or somebody reacting to a 429.
    """
    detail = record.get("reason") or "credential rotation"
    from_account = record.get("from_account")
    to_account = record.get("to_account")
    if from_account and to_account and from_account != to_account:
        detail = f"{detail} ({from_account} -> {to_account})"
    raw = dict(record)
    raw.update(
        {
            "event": TOKEN_ROTATED,
            "agent": None,  # a rotation hits every co-tenant, not one agent
            "account": to_account or from_account,
            "http_status": None,
            "detail": detail,
            "rotation_event": record.get("event"),
            "source": ROTATION_AUDIT_FILENAME,
        }
    )
    return AuthEvent(
        timestamp_utc=record.get("timestamp_utc"),
        event=TOKEN_ROTATED,
        agent=None,
        account=to_account or from_account,
        http_status=None,
        detail=detail,
        raw=raw,
    )


def rotation_events(path: Path | None = None) -> list[AuthEvent]:
    """Read the rotation audit, projected into auth-event shape.

    Returns ``[]`` when the audit is missing or unreadable. That empty answer
    means "we have no rotation record here", NOT "no rotation happened" — the
    two are different, and only the caller knows whether the rail was even
    installed for the window being investigated.
    """
    import json

    target = Path(path) if path is not None else rotation_audit_path()
    # stx-allow: fallback (reason: diagnostic read — a missing or unreadable
    # audit must degrade to an empty projection, never raise.)
    try:
        with target.open("r", encoding="utf-8") as handle:
            out: list[AuthEvent] = []
            for line in handle:
                try:
                    obj = json.loads(line)
                except Exception:  # stx-allow: fallback (reason: see above)
                    continue
                if isinstance(obj, dict):
                    out.append(_project(obj))
            return out
    except FileNotFoundError:
        return []
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return []


def unified_timeline(
    *,
    events_path: Path | None = None,
    audit_path: Path | None = None,
    include_rotations: bool = True,
) -> list[AuthEvent]:
    """Auth events + projected rotations, ordered oldest-first.

    Sorted by ``timestamp_utc`` as a STRING, which is correct here only
    because every writer emits timezone-aware ISO-8601 in UTC — same offset,
    same field widths, so lexical order is chronological order. A record with
    no timestamp sorts last rather than being dropped: a malformed record is
    still evidence that something wrote one.
    """
    events = list(read_auth_events(events_path))
    if include_rotations:
        events.extend(rotation_events(audit_path))
    return sorted(
        events, key=lambda e: (e.timestamp_utc is None, e.timestamp_utc or "")
    )
