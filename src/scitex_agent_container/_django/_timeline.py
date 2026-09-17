"""The fleet activity timeline — a projection of published runtime signals.

Card: sac-agent-activity-timeline-dashboard-20260917.

WHAT THIS OWNS. Only presentation of evidence the SAC control plane already
published. The build functions take the SAME ``/agents/<name>/status``
``activity`` blocks the detail view already projects, and flatten them into
timestamped entries. The GUI keeps no store, derives no lifecycle state, and
never reads a runtime directory or tmux — SAC stays SSOT, exactly as the card
requires.

THE ONE DISTINCTION THAT MATTERS. Three different facts must never render the
same way:

* **observed** — the runner published this signal. Show it.
* **stale** — the agent published before, and has since gone quiet. The last
  real observation is history worth showing, marked old.
* **unreachable** — we could not ask. This is NOT "no news"; it is an outage,
  and reporting it as quiet would hide real breakage behind silence.

``unknown`` is the fourth: asked successfully, nothing published. It is
deliberately not merged with ``stale`` — one means "the runner has no signal",
the other means "the runner HAD one and stopped".

DEDUP. A poll re-reads the same latest observation every time, so without
dedup a refresh would replay identical rows. The key includes the VALUE, so a
genuine change (``busy`` -> ``idle``) is a new event rather than being erased
as a duplicate.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

#: The signal families the runner publishes, in the order they render.
KINDS = (
    "phase",
    "operation",
    "turn_elapsed",
    "last_progress",
    "queue",
    "inference",
    "tool",
    "wait",
)

#: Bounded window: the timeline shows a recent slice, not an archive. A poll
#: must not grow the page with the square of the fleet.
DEFAULT_WINDOW = 200

#: Past this age an observation is no longer reported as current.
STALE_AFTER_SECONDS = 300.0


@dataclass(frozen=True)
class TimelineEntry:
    """One timestamped fact about one agent, as published."""

    agent: str
    kind: str
    value: Any
    state: str
    at: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dedupe_key(entry: TimelineEntry) -> tuple:
    """Identity of an event. Value is INCLUDED on purpose.

    Two polls that see the same unchanged observation must collapse to one
    row; a real state change must survive as a second row even when both
    observations land in the same second.
    """
    return (entry.agent, entry.kind, str(entry.value), entry.state, entry.at)


def dedupe_entries(entries: list[TimelineEntry]) -> list[TimelineEntry]:
    """Stable dedup, preserving first-seen order."""
    seen: set[tuple] = set()
    out: list[TimelineEntry] = []
    for entry in entries:
        key = _dedupe_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _entries_for(
    agent: str, status: Any, *, now: float, kind: str | None
) -> list[TimelineEntry]:
    """Flatten one agent's published activity block into entries."""
    if isinstance(status, BaseException):
        return [
            TimelineEntry(
                agent=agent,
                kind=kind or "operation",
                value=None,
                state="unreachable",
                at=now,
                source="",
            )
        ]
    if not isinstance(status, dict):
        return []
    activity = status.get("activity")
    if not isinstance(activity, dict):
        return []
    observed_at = status.get("observed_at")
    at = float(observed_at) if isinstance(observed_at, (int, float)) else now
    out: list[TimelineEntry] = []
    for name in KINDS:
        if kind is not None and name != kind:
            continue
        signal = activity.get(name)
        if not isinstance(signal, dict):
            continue
        if signal.get("state") != "observed":
            out.append(
                TimelineEntry(
                    agent=agent, kind=name, value=None, state="unknown", at=at, source=""
                )
            )
            continue
        # A published observation that has aged out is STALE: the fact is real
        # history, but it is no longer a current reading.
        state = "stale" if (now - at) > STALE_AFTER_SECONDS else "observed"
        out.append(
            TimelineEntry(
                agent=agent,
                kind=name,
                value=signal.get("value"),
                state=state,
                at=at,
                source=str(signal.get("source") or "")[:80],
            )
        )
    return out


def build_timeline(
    statuses: dict[str, Any],
    *,
    now: float | None = None,
    agent: str | None = None,
    kind: str | None = None,
    window: int = DEFAULT_WINDOW,
) -> list[TimelineEntry]:
    """Flat, newest-first, deduped, bounded timeline.

    ``statuses`` maps agent name to either its status dict or the exception
    raised while asking about it — the same shape the fleet view already
    collects, so the timeline adds no second read path.
    """
    moment = time.time() if now is None else now
    entries: list[TimelineEntry] = []
    for name, status in statuses.items():
        if agent is not None and name != agent:
            continue
        entries.extend(_entries_for(name, status, now=moment, kind=kind))
    entries = dedupe_entries(entries)
    entries.sort(key=lambda e: e.at, reverse=True)
    return entries[:window]


__all__ = [
    "DEFAULT_WINDOW",
    "KINDS",
    "STALE_AFTER_SECONDS",
    "TimelineEntry",
    "build_timeline",
    "dedupe_entries",
]
