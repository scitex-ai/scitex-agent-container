"""List resumable Claude Code conversations for an agent (#192, Part B #3).

When a ``--resume <uuid>`` target is gone ("No conversation found with
session ID: <uuid>") the recovery must be INFORMATIVE and SELECTABLE, not
a silent fresh start (operator design 4488):

  * Informative — list the conversations actually available for the agent
    (``$HOME/.claude/projects/<encoded-cwd>/*.jsonl``) with their
    timestamps and a first-message snippet, so the operator can see what
    is resumable.
  * Selectable — surface that candidate list so a ``--resume <chosen>``
    (or the explicit fresh-start last resort) is an informed choice.

The SDK (claude-agent-sdk / Claude Code) writes one ``<session-uuid>.jsonl``
per conversation under ``$HOME/.claude/projects/<encoded-resolved-cwd>/``,
where the dir name is the resolved working directory with ``/`` and ``.``
mapped to ``-`` (triple+ dashes collapsed to ``--``). This module reads
that store and returns structured candidates — no SDK import, pure stdlib,
unit-testable against a tmp ``$HOME``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "SessionCandidate",
    "encode_claude_project",
    "list_session_candidates",
    "format_candidates",
]


@dataclass(frozen=True)
class SessionCandidate:
    """One resumable conversation discovered in the SDK projects store.

    ``session_id`` is the ``.jsonl`` stem (the uuid a ``--resume`` takes).
    ``mtime`` is the file's last-modified unix time (newest = most recently
    active). ``first_message`` is a short snippet of the first user prompt
    (empty when the transcript has no parseable user turn).
    """

    session_id: str
    mtime: float
    first_message: str

    @property
    def mtime_iso(self) -> str:
        """ISO-8601 UTC timestamp of last activity (for human display)."""
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


def encode_claude_project(workdir: str) -> str:
    """Replicate Claude Code's cwd → ``projects/`` dir-name encoding.

    ``/`` and ``.`` both become ``-``; triple-or-more dashes (from hidden
    dirs like ``/.foo``) collapse back to ``--``. Mirrors
    ``_state._meta.transcript._encode_claude_project`` — duplicated here
    (one line of regex) so the runner-side candidate lister has no import
    dependency on the heavier ``agent_meta`` module.
    """
    encoded = workdir.replace("/", "-").replace(".", "-")
    return re.sub(r"-{3,}", "--", encoded)


def _project_dir(workdir: str, home: Path | None = None) -> Path:
    """Return the SDK projects dir for ``workdir`` under ``home``.

    Follows symlinks (Claude Code encodes the *resolved* cwd). ``home``
    defaults to ``Path.home()`` so the in-container runner finds its own
    store; tests pass a tmp home.
    """
    base = home or Path.home()
    try:
        resolved = str(Path(workdir).expanduser().resolve())
    except OSError:
        resolved = workdir
    return base / ".claude" / "projects" / encode_claude_project(resolved)


def _first_user_message(jsonl_path: Path, *, max_chars: int = 120) -> str:
    """Return a short snippet of the first user turn in a transcript.

    Best-effort: scans the ``.jsonl`` for the first record whose role /
    type marks a user message and pulls its text. Any IO / parse failure
    yields an empty string (the candidate is still listed by id + mtime).
    """
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _extract_user_text(rec)
                if text:
                    snippet = " ".join(text.split())
                    return snippet[:max_chars]
    except OSError:
        return ""
    return ""


def _extract_user_text(rec: dict) -> str:
    """Pull user-prompt text out of one transcript record, else ''.

    Handles the common Claude Code transcript shapes: a top-level
    ``{"type": "user", "message": {"content": ...}}`` and the simpler
    ``{"role": "user", "content": ...}``. ``content`` may be a plain
    string or a list of ``{"type": "text", "text": ...}`` blocks.
    """
    if not isinstance(rec, dict):
        return ""
    is_user = rec.get("type") == "user" or rec.get("role") == "user"
    if not is_user:
        return ""
    message = rec.get("message")
    content = (
        message.get("content") if isinstance(message, dict) else rec.get("content")
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def list_session_candidates(
    workdir: str,
    *,
    home: Path | None = None,
    limit: int = 10,
) -> list[SessionCandidate]:
    """Return resumable conversations for ``workdir``, newest first.

    Reads ``$HOME/.claude/projects/<encoded-workdir>/*.jsonl`` and returns
    up to ``limit`` :class:`SessionCandidate` rows sorted by mtime
    descending (most recently active first). Empty list when the projects
    dir is absent or holds no transcripts — the caller then knows there is
    genuinely nothing to resume.
    """
    proj = _project_dir(workdir, home=home)
    if not proj.is_dir():
        return []
    try:
        jsonls = sorted(
            proj.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    out: list[SessionCandidate] = []
    for p in jsonls[:limit]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        out.append(
            SessionCandidate(
                session_id=p.stem,
                mtime=mtime,
                first_message=_first_user_message(p),
            )
        )
    return out


def format_candidates(candidates: list[SessionCandidate]) -> str:
    """Render candidates as a human-readable, copy-pasteable list.

    Each line: ``  <session_id>  (<iso-mtime>)  <first-message snippet>``.
    Returns a sentinel line when there are no candidates so the caller's
    message is never an empty block.
    """
    if not candidates:
        return "  (no resumable conversations found in the projects store)"
    lines = [
        f"  {c.session_id}  ({c.mtime_iso})  {c.first_message}".rstrip()
        for c in candidates
    ]
    return "\n".join(lines)
