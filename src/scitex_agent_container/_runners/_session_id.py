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


def discard_dead_session(state_dir: Path, dead_id: str) -> bool:
    """Purge a *known-dead* session id from BOTH the latest marker and history.

    Unlike :func:`clear_session_id` (which clears only the latest marker
    and deliberately keeps the append-only history for audit), this is
    the recovery path for an id the SDK has confirmed is gone (resume
    rejected with "No conversation found with session ID"). Leaving a
    dead id in ``session_id_history`` makes a plain restart (and the
    supervisor's history-walking resume fallback in
    :mod:`._session_conversation`) RE-RESUME the dead uuid and
    RE-CRASH — the production crash-loop that left clew/neurovista mute
    for ~5h (2026-05-24).

    The whole history is first copied to ``session_id_history.dead-<ts>``
    so the audit trail is preserved, then:

    - the latest ``session_id`` marker is removed iff it equals
      ``dead_id`` (a newer valid id is left untouched), and
    - every line equal to ``dead_id`` is stripped from
      ``session_id_history`` (the file is removed when nothing valid
      remains).

    Returns True if anything was changed (marker cleared or a history
    line removed), False if ``dead_id`` was empty or appeared nowhere.

    Loud-by-design: the caller logs the discard. Never silently swallows
    an OSError other than the missing-file no-op (a busted runtime dir
    must surface).
    """
    if not dead_id:
        return False

    changed = False

    # Drop the latest marker only when it IS the dead id — a restart that
    # already wrote a fresher valid id must not be clobbered.
    if read_session_id(state_dir) == dead_id:
        changed = clear_session_id(state_dir) or changed

    history = read_session_id_history(state_dir)
    if dead_id not in history:
        return changed

    # Back up the full history before rewriting, so the dead id stays
    # auditable off to the side rather than vanishing.
    import time

    backup = state_dir / f"session_id_history.dead-{int(time.time())}"
    history_path = _session_id_history_path(state_dir)
    try:
        backup.write_text("\n".join(history) + "\n", encoding="utf-8")
    except OSError:
        # A failed backup must not block the recovery — the dead id MUST
        # be purged so the agent can self-heal. Losing the side-file
        # audit copy is the lesser evil; the discard itself is logged.
        pass

    survivors = [sid for sid in history if sid != dead_id]
    if survivors:
        tmp = state_dir / "session_id_history.tmp"
        tmp.write_text("\n".join(survivors) + "\n", encoding="utf-8")
        tmp.replace(history_path)
    else:
        try:
            history_path.unlink()
        except FileNotFoundError:
            pass
    return True


def clear_session_history(state_dir: Path) -> bool:
    """Clear the entire ``session_id_history`` (backed up first).

    The force-start / restart recovery path: a ``--force`` start means
    "I want a clean slate", so the resume fallback must not walk a
    history that may contain a dead id (the bug that made
    ``sac agents restart`` unable to recover a dead session — PR #190
    cleared only ``session_id``, leaving the dead uuid in history to be
    re-resumed). The whole history is copied to
    ``session_id_history.dead-<ts>`` before removal so nothing is lost.

    Returns True if a history file was removed, False if there was none.
    """
    history_path = _session_id_history_path(state_dir)
    if not history_path.is_file():
        return False
    history = read_session_id_history(state_dir)
    if history:
        import time

        backup = state_dir / f"session_id_history.dead-{int(time.time())}"
        try:
            backup.write_text("\n".join(history) + "\n", encoding="utf-8")
        except OSError:
            pass
    try:
        history_path.unlink()
        return True
    except FileNotFoundError:
        return False
