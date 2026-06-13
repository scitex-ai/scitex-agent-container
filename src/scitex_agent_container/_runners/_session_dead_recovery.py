# -*- coding: utf-8 -*-
"""Dead-session recovery for the conversation supervisor.

Extracted from :mod:`._session_conversation` so that file stays under the
project's per-file line cap. The supervisor's outer ``except`` block is
the only caller.

When the claude SDK rejects our ``--resume`` target with
"No conversation found with session ID: <uuid>", the conversation is
*recoverable*: walking ``session_id_history`` toward the same (or equally
dead, forked) id just re-crashes until ``max_restarts`` is exhausted and
the runner dies mute (the clew/neurovista 5h outage, 2026-05-24).
Instead, purge the dead id from BOTH the latest marker and the history,
then retry as a FRESH session (``resume=None``) — independent of the
``max_restarts`` budget.

:func:`handle_dead_session_resume` performs the purge + records the
``dead_session`` error and ``dead-session-fresh-start`` supervisor events
into ``session.jsonl``, then asks the caller (via the returned bool) to
reset its attempt counter and re-enter the supervisor loop. It returns
``False`` when the failure is NOT a dead-session resume — the caller
falls through to the auth/sdk-runtime classification.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Hard cap on dead-session recoveries per conversation lifetime. One per
# distinct dead id is the realistic ceiling (latest + every forked id in
# the history); the cap stops a pathological loop where ``discard``
# somehow fails to purge the id from spinning forever.
MAX_DEAD_SESSION_RECOVERIES = 5


def handle_dead_session_resume(
    *,
    enriched: str,
    state_dir: Path,
    name: str,
    host: str | None,
    attempt: int,
    current_sid: str | None,
    stderr_event_fields: dict[str, Any],
    append_session_message: Callable[[Path, dict], None],
    report_sdk_error: Callable[..., Any],
    db_writer: Any,
    stop_is_set: bool,
    dead_session_recoveries: int,
) -> bool:
    """Recover from an SDK ``--resume`` rejection, in place.

    Returns ``True`` when the failure was a dead-session resume and was
    handled (caller should reset its attempt counter to 0, clear its
    resume id, increment its own ``dead_session_recoveries``, and
    ``continue`` the supervisor loop). Returns ``False`` for any other
    failure (caller falls through to auth/sdk-runtime classification).
    """
    from ._dead_session import (
        DEAD_SESSION_CAUSE,
        extract_dead_session_id,
        is_dead_session_resume,
    )
    from ._session_id import discard_dead_session, read_session_id

    if (
        not is_dead_session_resume(enriched)
        or stop_is_set
        or dead_session_recoveries >= MAX_DEAD_SESSION_RECOVERIES
    ):
        return False

    # ``current_sid`` is the ground truth — it IS the resume target the
    # SDK just rejected. Prefer it; fall back to the id parsed out of the
    # error string, then to the on-disk marker, so we always have a
    # concrete id to purge.
    dead_id = (
        current_sid or extract_dead_session_id(enriched) or read_session_id(state_dir)
    )

    # INFORMATIVE (#192, Part B #3): before the last-resort fresh start,
    # enumerate the conversations that ARE resumable for this agent and
    # surface them — both to the log (LOUD) and to session.jsonl, so the
    # operator can `sac agents send --resume <chosen>` / restart with a
    # chosen id rather than only ever getting the silent fresh start.
    # The fresh start below is the documented LAST RESORT for the
    # autonomous runner (it cannot prompt interactively); the operator-
    # facing CLI path (`agents start --resume <uuid>`) is where the
    # choice is made synchronously (see
    # cli_pkg/lifecycle/_resume_preflight.py).
    from ._session_candidates import format_candidates, list_session_candidates

    candidates = list_session_candidates(os.getcwd())
    candidate_listing = format_candidates(candidates)
    logger.warning(
        "claude-session DEAD SESSION for %s: resume id %s is gone. "
        "Resumable conversations for this agent:\n%s\n"
        "Last resort: starting a FRESH session (resume=None). To resume "
        "a specific one instead, restart with "
        "`sac agents start %s --resume <session-id>`.",
        name,
        dead_id,
        candidate_listing,
        name,
    )
    if dead_id:
        discard_dead_session(state_dir, dead_id)
    append_session_message(
        state_dir,
        {
            "type": "error",
            "kind": "dead_session",
            "detail": enriched,
            "attempt": attempt,
            **stderr_event_fields,
        },
    )
    if host:
        report_sdk_error(
            name=name,
            host=host,
            cause=DEAD_SESSION_CAUSE,
            detail=enriched,
            db_writer=db_writer,
        )
    append_session_message(
        state_dir,
        {
            "type": "supervisor",
            "event": "dead-session-fresh-start",
            "discarded_session_id": dead_id,
            "resumable_candidates": [
                {
                    "session_id": c.session_id,
                    "mtime_iso": c.mtime_iso,
                    "first_message": c.first_message,
                }
                for c in candidates
            ],
        },
    )
    return True


__all__ = ["MAX_DEAD_SESSION_RECOVERIES", "handle_dead_session_resume"]

# EOF
