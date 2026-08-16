"""Persisted-session-id reset helper for the ``--force`` start path.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports ``_clear_persisted_session_id``.
"""

from __future__ import annotations

from pathlib import Path


def _runtime_state_dir(name: str) -> Path:
    """Resolve ``<runtime-root>/<name>`` from the env every call.

    Re-reads ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` each invocation (via
    the shared ``runtime_base_dir`` resolver) rather than capturing a
    module-level constant at import time, so tests (and callers that flip
    the env var per-process) see the fresh value without monkeypatching.
    """
    from .._runtime_paths import runtime_base_dir

    return runtime_base_dir() / name


def _clear_persisted_session_id(name: str) -> None:
    """Wipe BOTH the ``session_id`` marker AND ``session_id_history`` on ``--force``.

    Called from :func:`agent_start` on the ``--force`` path so a stale SDK
    resume marker can't ambush the next launch (see the module-level
    explanation at the call site).

    Clearing only ``session_id`` was insufficient: the runner's resume
    fallback walks the append-only ``session_id_history`` (see
    ``_session_conversation._resume_candidate``), so a dead uuid left in
    the history was RE-RESUMED on the next start and RE-CRASHED — the
    crash-loop that made ``sac agents restart`` unable to recover a dead
    session (clew/neurovista, 2026-05-24). A ``--force`` start means "clean
    slate", so the history is cleared too (backed up to
    ``session_id_history.dead-<ts>`` first by ``clear_session_history``).

    Loud-by-design: prints a ``[force]`` notice for each artifact actually
    removed. Missing files are a no-op (first-ever start). Any other OS
    error is allowed to propagate — the operator wants to know about a
    busted runtime dir, not have it silently swallowed.
    """
    from .._logging import get_logger
    from .._runners._session_state import clear_session_history, clear_session_id

    logger = get_logger(__name__)
    state_dir = _runtime_state_dir(name)
    removed_marker = clear_session_id(state_dir)
    removed_history = clear_session_history(state_dir)
    if removed_marker:
        logger.warning(
            f"[force] cleared persisted session_id for {name!r} "
            f"({state_dir / 'session_id'})"
        )
    if removed_history:
        logger.warning(
            f"[force] cleared session_id_history for {name!r} "
            f"(backed up to {state_dir}/session_id_history.dead-*)"
        )
