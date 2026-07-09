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

#: Sensible tail-like default for the trailing-messages preview when the
#: caller (CLI ``-n``/``--tail-lines``) omits it — a couple of lines reads
#: better than a single message in the existing one-line-per-candidate list
#: format (sac-session-candidates-tail-preview).
DEFAULT_TAIL_LINES = 2


@dataclass(frozen=True)
class SessionCandidate:
    """One resumable conversation discovered in the SDK projects store.

    ``session_id`` is the ``.jsonl`` stem (the uuid a ``--resume`` takes).
    ``mtime`` is the file's last-modified unix time (newest = most recently
    active). ``first_message`` is a short snippet of the first user prompt
    (empty when the transcript has no parseable user turn) — kept for
    callers that still want "how did this conversation start". The
    default-DISPLAYED preview is ``last_messages`` (the trailing N
    messages, more identifying for "what was I last doing" when choosing a
    session to resume — sac-session-candidates-tail-preview).
    """

    session_id: str
    mtime: float
    first_message: str
    last_messages: str = ""

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


def _extract_message_text(rec: dict) -> str:
    """Pull message text out of one transcript record, else ''.

    Like :func:`_extract_user_text` but accepts BOTH ``user`` and
    ``assistant`` turns — the tail preview wants "what was said last in
    this conversation" regardless of who said it, unlike the first-message
    snippet which specifically anchors on the operator's opening prompt.
    Returns the text prefixed with ``"<role>: "`` so a multi-line preview
    stays legible about who said what.
    """
    if not isinstance(rec, dict):
        return ""
    role = rec.get("type") or rec.get("role")
    if role not in ("user", "assistant"):
        return ""
    message = rec.get("message")
    content = (
        message.get("content") if isinstance(message, dict) else rec.get("content")
    )
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        text = " ".join(parts)
    else:
        text = ""
    text = " ".join(text.split())
    if not text:
        return ""
    return f"{role}: {text}"


def _last_messages(
    jsonl_path: Path,
    *,
    n: int = DEFAULT_TAIL_LINES,
    max_chars: int = 120,
) -> str:
    """Return a snippet of the last ``n`` messages in a transcript.

    Mirrors :func:`_first_user_message`'s parsing + per-message
    char-truncation convention, but scans the ``.jsonl`` BACKWARD from the
    end and pulls text from both user and assistant turns (the trailing
    exchange is what "what was I last doing" needs, not just the
    operator's own last prompt). Each matched message is truncated to
    ``max_chars`` and the (up to) ``n`` snippets are joined with ``" | "``,
    oldest-first, so the result stays a single line in the existing
    one-line-per-candidate list format. Best-effort: any IO / parse
    failure yields an empty string, same as the first-message extractor.
    """
    if n <= 0:
        return ""
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    snippets: list[str] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _extract_message_text(rec)
        if text:
            snippets.append(text[:max_chars])
            if len(snippets) >= n:
                break
    return " | ".join(reversed(snippets))


def list_session_candidates(
    workdir: str,
    *,
    home: Path | None = None,
    limit: int = 10,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> list[SessionCandidate]:
    """Return resumable conversations for ``workdir``, newest first.

    Reads ``$HOME/.claude/projects/<encoded-workdir>/*.jsonl`` and returns
    up to ``limit`` :class:`SessionCandidate` rows sorted by mtime
    descending (most recently active first). Empty list when the projects
    dir is absent or holds no transcripts — the caller then knows there is
    genuinely nothing to resume.

    ``tail_lines`` controls how many TRAILING messages are captured into
    each candidate's ``last_messages`` preview (the default DISPLAYED
    snippet — see :func:`format_candidates`); pass ``0`` to skip tail
    extraction entirely. ``first_message`` is always computed too, for
    callers still relying on it.
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
                last_messages=_last_messages(p, n=tail_lines),
            )
        )
    return out


def format_candidates(candidates: list[SessionCandidate]) -> str:
    """Render candidates as a human-readable, copy-pasteable list.

    Each line: ``  <session_id>  (<iso-mtime>)  <last-messages preview>``.
    The preview shows the TRAILING messages of the transcript (what the
    conversation was last doing — more identifying than the opening
    prompt when choosing which session to resume,
    sac-session-candidates-tail-preview) rather than the first-message
    snippet. Returns a sentinel line when there are no candidates so the
    caller's message is never an empty block.
    """
    if not candidates:
        return "  (no resumable conversations found in the projects store)"
    lines = [
        f"  {c.session_id}  ({c.mtime_iso})  "
        f"{c.last_messages or c.first_message}".rstrip()
        for c in candidates
    ]
    return "\n".join(lines)
