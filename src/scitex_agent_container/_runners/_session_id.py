"""Session-id resume marker + append-only history.

Extracted from ``_runners/_session_state.py`` (which re-exports this
surface for back-compat) to keep that module under the 512-line cap and
to give the session-id concern one focused home.

Two on-disk artifacts under ``<state_dir>/``:

- ``session_id``         — the single, overwritten *latest* resume marker.
- ``session_id_history`` — append-only, one distinct id per line.

The SDK may **fork** the session id on a resume (return a new id instead
of the one we asked it to resume). The latest marker advances to the
fork, but the prior id's transcript is still on disk; the history keeps
that orphaned id auditable and resumable. See ``_session_conversation``
for the fork-detection log and the resume fallback that walks the
history when the latest id is rejected.

Atomic writes use the tmp + ``Path.replace`` pattern so a concurrent
reader (``sac agent status``) never sees a half-formed file. The history
is append-only, so a plain append is already crash-consistent at line
granularity.
"""

from __future__ import annotations

from pathlib import Path


def _session_id_history_path(state_dir: Path) -> Path:
    return state_dir / "session_id_history"


def append_session_id_history(state_dir: Path, session_id: str) -> bool:
    """Append ``session_id`` to the append-only history when it is distinct.

    The history (``<state_dir>/session_id_history``, one id per line) lets
    an orphaned-but-on-disk session id stay auditable and resumable even
    after the SDK forks the resume marker on a resume (see
    :func:`write_session_id`). Appends only when ``session_id`` differs
    from the most recent recorded id, so re-writing the same id every
    turn does not bloat the file.

    Returns True if a new line was appended, False if it was a duplicate
    of the current tail (or the id was empty).
    """
    if not session_id:
        return False
    history = read_session_id_history(state_dir)
    if history and history[-1] == session_id:
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    with _session_id_history_path(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(session_id + "\n")
    return True


def read_session_id_history(state_dir: Path) -> list[str]:
    """Return every distinct session id recorded, oldest first.

    Empty list when no history exists. The last element is the most
    recently recorded id (which should match :func:`read_session_id`).
    """
    p = _session_id_history_path(state_dir)
    if not p.is_file():
        return []
    try:
        return [
            line.strip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return []


def write_session_id(state_dir: Path, session_id: str) -> None:
    """Persist the SDK session id so a respawn can resume.

    Also appends the id to the append-only ``session_id_history`` when it
    is distinct from the last recorded one, so a forked/orphaned id is
    never lost: the latest marker is overwritten, but the history is
    additive.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "session_id.tmp"
    tmp.write_text(session_id, encoding="utf-8")
    tmp.replace(state_dir / "session_id")
    append_session_id_history(state_dir, session_id)


def read_session_id(state_dir: Path) -> str | None:
    """Return the persisted session id, or None if absent."""
    p = state_dir / "session_id"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def clear_session_id(state_dir: Path) -> bool:
    """Remove the persisted ``session_id`` resume marker.

    Used by ``agent_start(force=True)`` so a stale session id left over
    from a previous run can't make the SDK try to resume a conversation
    the server has already aged out (symptom: ``ProcessError: Command
    failed with exit code 1`` ~90s into the first turn).

    Returns True if a file was removed, False if there was nothing to
    remove. Never raises FileNotFoundError; never silently swallows
    other ``OSError``s (callers want a loud failure if e.g. the runtime
    dir is unreadable due to permissions).

    Note: this clears only the *latest* marker, not the append-only
    ``session_id_history`` — the history is intentionally durable so a
    forced fresh start can still audit which ids preceded it.
    """
    p = state_dir / "session_id"
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
