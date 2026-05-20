"""Persisted-session-id reset helper for the ``--force`` start path.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports ``_clear_persisted_session_id``.
"""

from __future__ import annotations

from pathlib import Path


def _clear_persisted_session_id(name: str) -> None:
    """Wipe ``<runtime-root>/<name>/session_id`` if present.

    Called from :func:`agent_start` on the ``--force`` path so a stale
    SDK resume marker can't ambush the next launch (see the module-level
    explanation at the call site).

    The runtime root is re-read from ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
    every invocation rather than reused from the module-level constant
    captured at import time, so tests (and callers that flip the env
    var per-process) see the fresh value without monkeypatching.

    Loud-by-design: prints a ``[force]`` notice when a file was actually
    removed. Missing file is a no-op (first-ever start). Any other OS
    error is allowed to propagate — the operator wants to know about a
    busted runtime dir, not have it silently swallowed.
    """
    import os
    import sys

    from .._runners._session_state import clear_session_id

    runtime_root = Path(
        os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
            str(Path.home() / ".scitex" / "agent-container" / "runtime"),
        )
    )
    state_dir = runtime_root / name
    removed = clear_session_id(state_dir)
    if removed:
        print(
            f"[force] cleared persisted session_id for {name!r} "
            f"({state_dir / 'session_id'})",
            file=sys.stderr,
        )
