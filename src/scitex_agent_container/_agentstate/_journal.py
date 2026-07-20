"""Persist every state reading — RAW captures included — to a greppable JSONL.

「状態とった時に全ログを取っておいてくださいね？」

WHY RAW, AND WHY NOT SUMMARIES
    Every diagnosis during the 2026-07-17/18 incident that reached a true answer
    did so because somebody had kept RAW TEXT: a pane capture with an embedded
    timestamp, an ``age=`` field, a rotation audit line naming ``from_account``.
    Every diagnosis that went wrong had only verdicts — "auth-status says OK",
    "169 restarts logged", "0 wedged" — and each of those turned out to be either
    false or unfalsifiable. **A verdict cannot be re-examined after the fact. A
    capture can.** So this file stores what was SEEN, and the verdict merely
    travels alongside it.

    The question this exists to make answerable is "what did this agent's pane
    look like at 20:20?", answered by ``grep`` months later rather than by
    re-running a probe that can no longer see the past.

SIZE IS BOUNDED, BUT NEVER BY SUMMARISING
    A full pane per agent per pass grows without limit, so this rotates and caps.
    Two rules govern how:

    1. **A truncation is always MARKED** — in the text itself and in a
       ``truncated`` record carrying the original byte count. A capture that
       looks complete but is not is strictly worse than one that admits it was
       cut, because a reader draws confident conclusions from the part they can
       see. (This is the rtk filtered-diff failure in another costume: content
       silently dropped, with no marker, so the reader cannot know to doubt it.)
    2. **Rotation MOVES bytes, it never condenses them.** The previous
       generation is renamed, not summarised. Solving size by storing summaries
       would reintroduce precisely the problem this file exists to fix.

WRITING NEVER BREAKS THE READING
    A journal failure returns a :class:`JournalWrite` saying so; it does not
    raise. Losing the observation because the disk is full would be a worse
    outcome than losing the archive of it — but the failure is REPORTED, never
    swallowed, because a log that quietly stopped writing is the "guard nobody
    executed" pattern, and it is indistinguishable from a quiet fleet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ._assess import assess
from ._state import AgentState

__all__ = [
    "DEFAULT_MAX_CAPTURE_BYTES",
    "DEFAULT_MAX_JOURNAL_BYTES",
    "JournalWrite",
    "append_state",
    "journal_path",
    "mark_truncated",
    "read_journal",
]

#: Per-capture cap. A Claude TUI pane is a few KB; 256 KB keeps whole captures
#: whole with generous headroom, and only a pathological capture is ever cut.
DEFAULT_MAX_CAPTURE_BYTES = 256 * 1024

#: Rotate at 64 MB. One generation is kept (``.1``), so the archive costs at
#: most ~128 MB — cheap next to the cost of one more unfalsifiable outage.
DEFAULT_MAX_JOURNAL_BYTES = 64 * 1024 * 1024

#: The marker appended to a cut capture. Deliberately unmistakable and greppable:
#: a reader scanning for suspicious evidence can find every truncated record with
#: one search, and an automated consumer can key off the same string.
TRUNCATION_MARKER = "\n<<<SAC-AGENTSTATE-TRUNCATED kept={kept}B of {total}B>>>"


def journal_path() -> Path:
    """``<runtime>/agent-state.jsonl`` — beside auth-heal.log, not somewhere new.

    Resolved per call, never captured into a module-level constant: a
    ``Path.home()``-derived constant computed at import cannot be redirected by a
    fixture that sets ``$HOME``, and that exact bug once had a suite in this repo
    reading and WRITING the real fleet registry.
    """
    override = os.environ.get("SAC_AGENT_STATE_JOURNAL")
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "agent-state.jsonl"


@dataclass(frozen=True)
class JournalWrite:
    """What the write did — including whether anything had to be cut."""

    ok: bool
    path: Path | None = None
    detail: str = ""
    truncated: tuple[str, ...] = ()
    rotated: bool = False


def mark_truncated(text: str, limit: int) -> tuple[str, bool, int]:
    """Cut ``text`` to ``limit`` BYTES, marking the cut. ``(text, cut?, total)``.

    Counts BYTES, not characters, because the cap exists to bound a file on disk
    and a multi-byte capture would otherwise blow past a character budget. The
    cut is made on a UTF-8 boundary (``errors="ignore"`` on the decode drops a
    split trailing sequence rather than emitting a replacement character), and
    the marker states BOTH the kept and the original size so a reader can see
    exactly how much is missing rather than merely that something is.
    """
    encoded = text.encode("utf-8")
    total = len(encoded)
    if total <= limit:
        return text, False, total
    kept = encoded[:limit].decode("utf-8", errors="ignore")
    return (
        kept + TRUNCATION_MARKER.format(kept=len(kept.encode("utf-8")), total=total),
        True,
        total,
    )


def _rotate(path: Path, max_bytes: int) -> bool:
    """Move the journal aside once it is large. Returns whether it rotated.

    One generation, and it is a RENAME: the bytes survive intact under
    ``<name>.1``. Nothing here condenses, samples or summarises the outgoing
    file, because the whole reason this archive exists is that summaries cannot
    be re-examined.
    """
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            path.replace(path.with_suffix(path.suffix + ".1"))
            return True
    except OSError:  # stx-allow: fallback (reason: a rotation we cannot perform must not stop the append — an oversized journal still holds the evidence, whereas a refused write loses it)
        return False
    return False


def append_state(
    state: AgentState,
    *,
    path: Path | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    max_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
) -> JournalWrite:
    """Append ONE state — signals, reasons, verdict and RAW captures — as JSONL.

    The record is one line so ``grep`` works, and it carries ``raw`` in full
    (subject to the marked cap) so a later question about what a pane actually
    contained is answerable from the file rather than from a probe that can no
    longer see that moment.
    """
    target = path if path is not None else journal_path()
    rotated = _rotate(target, max_bytes)

    raw: dict[str, Any] = {}
    truncated: list[str] = []
    for key, value in state.raw.items():
        text, was_cut, total = mark_truncated(str(value), max_capture_bytes)
        raw[key] = text
        if was_cut:
            truncated.append(key)
            # The cut is recorded as DATA as well as inline text, so a consumer
            # parsing JSON sees it without having to scan for the marker string.
            raw[f"{key}__truncated"] = json.dumps(
                {"kept_bytes": max_capture_bytes, "original_bytes": total}
            )

    record = state.to_dict()
    record["raw"] = raw
    record["truncated"] = truncated
    record["assessment"] = assess(state).to_dict()

    # stx-allow: fallback (reason: the OBSERVATION is the valuable thing and must survive a journal failure; the failure is RETURNED so the caller reports it loudly rather than it being swallowed)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        return JournalWrite(
            ok=False,
            path=target,
            detail=(
                f"could not append the state reading to {target}: {exc}. The "
                f"reading itself is intact and was returned to the caller; what "
                f"is lost is the ARCHIVE of it, so this observation will not be "
                f"re-examinable later"
            ),
            truncated=tuple(truncated),
            rotated=rotated,
        )

    return JournalWrite(
        ok=True,
        path=target,
        detail=f"appended 1 state record to {target}",
        truncated=tuple(truncated),
        rotated=rotated,
    )


def read_journal(path: Path | None = None) -> Iterator[dict]:
    """Yield the journal's records, oldest first. Skips nothing silently.

    A line that will not parse is yielded as ``{"_unparseable": <raw line>}``
    rather than dropped: a torn write is itself evidence about the moment it was
    written, and an iterator that silently skips damaged records is an instrument
    reporting a cleaner history than the one on disk.
    """
    target = path if path is not None else journal_path()
    # stx-allow: fallback (reason: an absent journal is "nothing recorded yet", which is a legitimate empty read, not an error to raise at a reader)
    try:
        handle = target.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:  # stx-allow: fallback (reason: a torn line is evidence too — surfaced as data, never dropped)
                yield {"_unparseable": line}


# EOF
