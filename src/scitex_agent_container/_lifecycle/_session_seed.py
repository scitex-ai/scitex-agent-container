"""Spec-pinned session-id seeding for the SDK runner (``session: resume``).

``spec.claude.resume_id`` lets an operator PIN an agent's first SDK
session to an existing transcript uuid (migration / hand-off). The SDK
runner resumes by reading ``<state_dir>/session_id`` and passing
``ClaudeAgentOptions(resume=<id>)`` — but it has no "seed a fresh
arbitrary id" knob; it only RESUMES an id already on disk. So to honour
the pin we must WRITE that marker file ourselves before the runtime
starts.

Seed-if-ABSENT is the load-bearing semantic. After the first turn the
SDK may FORK the session id (return a new uuid), and
``write_session_id`` advances ``<state_dir>/session_id`` to the fork —
that forked id is the live thread. Re-seeding from ``resume_id`` on
every restart would clobber the fork back to the original pin and lose
the whole continuation. So we seed ONLY when no marker exists yet (first
boot / migration); once a marker is present it is authoritative.

Called from :func:`._start.agent_start` BEFORE ``runtime.start`` so the
in-container SDK runner (which reads the bind-mounted state dir) sees the
seeded id on its first resume attempt.
"""

from __future__ import annotations

from typing import Any

from ..config import AgentConfig
from ..config._session_continuity import SESSION_RESUME


def seed_pinned_session_id(config: AgentConfig, runtime: Any) -> bool:
    """Seed ``<state_dir>/session_id`` from ``spec.claude.resume_id`` if absent.

    No-op (returns False) unless ALL hold:
      * ``config.claude.session == "resume"``,
      * ``config.claude.resume_id`` is a non-empty string, and
      * ``<state_dir>/session_id`` is currently ABSENT (first boot /
        migration — never overwrite an existing, possibly forked, id).

    Returns True iff a seed was written.
    """
    claude = getattr(config, "claude", None)
    if claude is None:
        return False
    session = str(getattr(claude, "session", "") or "").strip().lower()
    if session != SESSION_RESUME:
        return False
    resume_id = str(getattr(claude, "resume_id", "") or "").strip()
    if not resume_id:
        return False

    # Resolve the SAME per-agent state dir the SDK runner reads its resume
    # marker from — via the runtime's own ``_state_dir`` resolver (root can
    # be a project-scoped runtime dir, not the global default). Reuses the
    # shared helper so seeding and instance-row bookkeeping never diverge.
    from ._instances import _state_dir_for

    state_dir = _state_dir_for(config, runtime)
    if state_dir is None:
        return False

    from .._runners._session_state import read_session_id, write_session_id

    # Fork-preservation: an existing marker (the latest, possibly forked,
    # live id) is authoritative — never re-pin it back to the original.
    if read_session_id(state_dir) is not None:
        return False

    write_session_id(state_dir, resume_id)
    return True
