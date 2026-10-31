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

import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

#: The signal families the runner publishes, in the order they render.
LEGACY_KINDS = (
    "phase",
    "operation",
    "turn_elapsed",
    "last_progress",
    "queue",
    "inference",
    "tool",
    "wait",
)

# Native lifecycle counters are snapshots, never individual tool events or
# evidence that a tool succeeded. Capacity has no supported measurement type.
NATIVE_LABELS = {
    "turns_accepted": "Turns accepted",
    "turns_completed": "Turns completed",
    "tools_started": "Native tool calls started",
    "tools_completed": "Native tool lifecycles completed",
    "tools_inflight": "Native tool calls in flight",
    "last_turn_status": "Last turn status",
    "last_error_code": "Last error code",
    "session_id": "Native session",
    "capacity": "Provider capacity",
}
KINDS = LEGACY_KINDS + tuple(NATIVE_LABELS)
NATIVE_PROGRESS_SOURCE = "heartbeat.authoritative_heartbeat.progress_at"

#: Bounded window: the timeline shows a recent slice, not an archive. A poll
#: must not grow the page with the square of the fleet.
DEFAULT_WINDOW = 200

#: Past this age an observation is no longer reported as current.
STALE_AFTER_SECONDS = 300.0

#: How often the browser re-asks. A FLOOR, not a rate: the poller schedules the
#: next request only after the previous one settles, so a slow control plane
#: (this fleet measures 5-60s per read) cannot stack requests.
REFRESH_SECONDS = 15


@dataclass(frozen=True)
class TimelineEntry:
    """One timestamped fact about one agent, as published."""

    agent: str
    kind: str
    value: Any
    state: str
    at: float | None
    source: str
    reason: str = ""
    note: str = ""
    snapshot: bool = False
    native_session: str = ""
    native_session_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        """The client-visible dedup key.

        ``timeline.js`` dedupes on EXACTLY this value, so the two layers agree
        by construction; if they computed the key differently the page would
        duplicate rows the API considers identical.
        """
        timestamp = f"{self.at:.3f}" if self.at is not None else "undated"
        return "|".join((self.agent, self.kind, str(self.value), self.state, timestamp))


_STATE_LABELS = {
    "observed": "observed",
    "stale": "stale",
    "unreachable": "unreachable",
    "unknown": "unknown",
}


def timeline_rows(entries: list[TimelineEntry], *, now: float | None = None) -> list[dict]:
    """Presentation rows for the template: adds key, label and relative time.

    Formatting only — no value is added, changed or inferred here.
    """
    moment = time.time() if now is None else now
    rows: list[dict] = []
    for entry in entries:
        age = max(0, int(moment - entry.at)) if entry.at is not None else None
        if age is None:
            relative = "Age unknown"
        elif age < 60:
            relative = f"{age}s ago"
        elif age < 3600:
            relative = f"{age // 60}m ago"
        else:
            relative = f"{age // 3600}h ago"
        row = entry.as_dict()
        row["key"] = entry.key
        row["state_label"] = _STATE_LABELS.get(entry.state, entry.state)
        native_progress = entry.kind == "last_progress" and entry.source == NATIVE_PROGRESS_SOURCE
        row["kind_label"] = "Last native progress event" if native_progress else NATIVE_LABELS.get(entry.kind, entry.kind)
        row["static"] = entry.snapshot or native_progress
        row["display_value"] = (
            "Unknown" if (entry.snapshot or native_progress) and entry.value is None else entry.value
        )
        if entry.snapshot and type(entry.value) is int:
            row["display_value"] = str(entry.value)  # Preserve counter precision in browser JSON.
        row["snapshot_key"] = (
            json.dumps([entry.agent, entry.kind]) if entry.snapshot else ""
        )
        # Presentation state ages from observed to stale without creating a
        # second event. Preserve value, source and the original event time.
        dated = (
            not isinstance(entry.at, bool) and isinstance(entry.at, (int, float))
            and math.isfinite(entry.at) and 0 < entry.at <= moment
        )
        row["event_key"] = (
            json.dumps([entry.agent, entry.kind, entry.value, entry.at, entry.source], default=str)
            if dated and not entry.snapshot else ""
        )
        row["relative"] = relative
        row["age_seconds"] = age
        row["iso"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.at))
            if entry.at is not None else ""
        )
        rows.append(row)
    return rows



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


def _observation_time(value: Any, now: float) -> float | None:
    """Only a usable published timestamp establishes the observation's age."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = float(value)
    except OverflowError:
        return None
    return at if math.isfinite(at) and 0 < at <= now else None


def _native_progress_time(value: Any, now: float) -> float | None:
    """An explicit timezone-bearing native event time, never poll/mtime time."""
    if not isinstance(value, str) or not 20 <= len(value) <= 128 or value[10] != "T":
        return None
    try:
        event = datetime.fromisoformat(value)
        if event.tzinfo is None or event.utcoffset() is None:
            return None
        return _observation_time(event.timestamp(), now)
    except (ValueError, OverflowError, OSError):
        return None


def _native_entry(agent: str, name: str, signal: Any, at: float | None, now: float) -> TimelineEntry:
    """Validate one typed native snapshot, keeping its provenance and limits."""
    if not isinstance(signal, dict):
        return TimelineEntry(agent, name, None, "unknown", None, "",
                             note="No usable native observation is published.", snapshot=True)
    source = signal.get("source")
    source = source if isinstance(source, str) else ""
    reason = signal.get("reason")
    reason = reason if isinstance(reason, str) else ""
    if "observed_at" in signal:
        at = _observation_time(signal["observed_at"], now)
    published_state = signal.get("state")
    supported_state = isinstance(published_state, str) and published_state in {
        "observed", "stale", "unreachable"
    }
    value = signal.get("value")
    if name in {"turns_accepted", "turns_completed", "tools_started", "tools_completed", "tools_inflight"}:
        valid = type(value) is int and value >= 0
    else:
        valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and not any(ord(c) < 32 for c in value)
    note = ""
    if name == "capacity":
        # The contract offers an explicit unknown, with no units or source for
        # an authoritative provider measurement. Never interpret a number here.
        value, state = None, "unknown"
        if not reason:
            note = "No supported authoritative capacity measurement is published."
    elif not supported_state or not valid or not source.strip():
        value = None
        state = published_state if published_state in ("stale", "unreachable") else "unknown"
        if not reason:
            note = "No usable native observation is published."
    elif published_state != "observed":
        state = published_state
    elif at is None:
        state = "unknown"
    else:
        state = "stale" if now - at > STALE_AFTER_SECONDS else "observed"
    return TimelineEntry(agent, name, value, state, at, source, reason, note, True)


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
    at = _observation_time(status.get("observed_at"), now)
    out: list[TimelineEntry] = []
    native = any(name in activity for name in NATIVE_LABELS)
    session = _native_entry(agent, "session_id", activity.get("session_id"), at, now) if native else None
    for name in KINDS:
        if kind is not None and name != kind:
            continue
        signal = activity.get(name)
        if name in NATIVE_LABELS:
            if native:
                entry = _native_entry(agent, name, signal, at, now)
                out.append(replace(entry, native_session=session.value or "",
                                   native_session_at=session.at if session.value else None))
            continue
        if not isinstance(signal, dict):
            continue
        published_state = signal.get("state")
        native_progress = name == "last_progress" and signal.get("source") == NATIVE_PROGRESS_SOURCE
        reason = signal.get("reason") if native_progress and isinstance(signal.get("reason"), str) else ""
        entry_at = at
        if native_progress:
            entry_at = _native_progress_time(signal.get("value"), now) if published_state == "observed" else None
        if not isinstance(published_state, str) or published_state not in {
            "observed", "stale", "unreachable"
        }:
            out.append(
                TimelineEntry(
                    agent=agent, kind=name, value=None, state="unknown", at=entry_at,
                    source=NATIVE_PROGRESS_SOURCE if native_progress else "", reason=reason,
                )
            )
            continue
        # A published observation that has aged out is STALE: the fact is real
        # history, but it is no longer a current reading.
        # Poll time is transport freshness, not runtime evidence. Without a
        # timestamp, preserve the value as undated but never call it current.
        # Explicit source degradation must also survive a recent timestamp.
        if published_state != "observed":
            state = published_state
        elif entry_at is None:
            state = "unknown"
        else:
            state = "stale" if (now - entry_at) > STALE_AFTER_SECONDS else "observed"
        out.append(
            TimelineEntry(
                agent=agent,
                kind=name,
                value=None if native_progress and entry_at is None and published_state == "observed" else signal.get("value"),
                state=state,
                at=entry_at,
                source=str(signal.get("source") or "")[:80],
                reason=reason,
                note="Native progress event time is unknown." if native_progress and entry_at is None and not reason else "",
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
    entries.sort(key=lambda e: e.at if e.at is not None else -math.inf, reverse=True)
    return entries[:window]


__all__ = [
    "DEFAULT_WINDOW",
    "KINDS",
    "NATIVE_LABELS",
    "REFRESH_SECONDS",
    "STALE_AFTER_SECONDS",
    "TimelineEntry",
    "build_timeline",
    "dedupe_entries",
    "timeline_rows",
]
