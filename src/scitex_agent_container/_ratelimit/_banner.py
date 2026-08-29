"""Is this pane PAUSED behind a provider rate wall, and until WHEN?

PURE — no tmux, no clock of its own, no I/O. Every input is passed in, so
the whole table below is exercised against real captured panes.

WHY A SEPARATE MATCHER FROM ``auth_status.banner_kind``
------------------------------------------------------
That matcher deliberately EXCLUDES this case, and says so at the line where
it excludes it: ``429 (rate limit) is deliberately excluded — a restart does
not fix a rate wall``. It is right. A rate wall is not a wedge and not a
death: it is a PAUSE WITH A PUBLISHED END TIME, and the only correct
response is to wait for that time and then carry on. Restarting during the
wall burns the agent's context and hits the same wall on the next turn.

So this module answers a different question from the auth matcher, and the
two populations are disjoint by construction: an auth banner names a
credential problem, this one names a quota window.

THE SPECIMENS THIS IS BUILT ON — real captures, not invented
------------------------------------------------------------
Recovered from agent transcripts on 2026-08-28 (the ``⎿`` result marker and
the NBSP after it are exactly the rendering ``auth_status._MARKERS`` already
strips, which is why this module reuses that stripper rather than growing a
second one)::

    ⎿ You've hit your weekly limit · resets 8am (UTC)
       /usage-credits to finish what you're working on.

    You've hit your weekly limit · resets 11pm (UTC)
    You've hit your weekly limit · resets 8am (Asia/Tokyo)
    You've hit your weekly limit · resets 5pm (Asia/Tokyo)

and the phrasing the operator quoted from the 2026-08-28 17:25 UTC incident::

    You've hit your session limit · resets 7:10pm

The window word varies (``weekly`` / ``session`` / ``usage``); the shape does
not. The apostrophe arrives as ASCII ``'`` in some captures and U+2019 in
others, so both are matched — a detector that silently stopped matching on a
typographic quote would report a healthy fleet during an outage.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It never GUESSES a reset time. A banner whose reset clause it cannot parse
yields ``reset_at=None`` — a distinct, reportable state — and the rule above
it holds rather than resuming. Inventing a reset is precisely how a reviver
starts hammering a wall that is still up, which costs quota and extends the
outage it was built to end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "LIMIT_RE",
    "LimitObservation",
    "SPECIMEN_PANES",
    "observe_pane",
    "parse_reset_at",
    "resolve_clock_near",
]

#: The banner itself. Anchored on "you<'>ve hit your <word> limit" so agent
#: PROSE about a rate limit ("figrecipe died behind a weekly limit") does not
#: match: the anchor is the provider's own first-person sentence, which an
#: agent writing about the incident does not reproduce verbatim at the start
#: of a line. Both apostrophes; case-insensitive.
LIMIT_RE = re.compile(
    r"you['’]ve\s+hit\s+your\s+(?P<window>[a-z0-9-]+)\s+limit",
    re.IGNORECASE,
)

#: The reset clause. ``resets 8am (UTC)`` / ``resets 7:10pm`` /
#: ``resets 23:00 (Asia/Tokyo)`` / ``resets at 2026-06-18T05:00Z``. The
#: timezone is OPTIONAL in the rendering and therefore optional here; when it
#: is absent the caller supplies the frame it is reading the pane in, which is
#: the only defensible substitute for a label the provider did not print.
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(?P<clock>"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?Z?"  # ISO instant
    r"|\d{1,2}(?::\d{2})?\s*(?:am|pm)"  # 8am / 7:10pm
    r"|\d{1,2}:\d{2}"  # 23:00
    r")\s*(?:\((?P<tz>[^)]{1,40})\))?",
    re.IGNORECASE,
)

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z?)$")
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)

#: Verbatim captures, kept in the module they justify so a future reader can
#: see WHAT the patterns above were fitted to without hunting a transcript.
#: The tests assert against these, so a rendering change that breaks the
#: matcher breaks a test rather than silently returning "not limited".
SPECIMEN_PANES: tuple[str, ...] = (
    "⎿ You've hit your weekly limit · resets 8am (UTC)",
    "You've hit your weekly limit · resets 11pm (UTC)",
    "You've hit your weekly limit · resets 8am (Asia/Tokyo)",
    "You’ve hit your session limit · resets 7:10pm",
)


@dataclass(frozen=True)
class LimitObservation:
    """One pane, read once. THREE states, never two.

    ``readable=False`` is not ``limited=False``. A pane we could not capture
    told us nothing about that agent, and reporting "not limited" there would
    be an instrument announcing good news about something it never saw — the
    same distinction :class:`.._authheal._detect.Roster` draws for the roster
    and :class:`.._reconcile._budget.HistoryRead` for the ledger.

    ``line_index`` is the pane line the banner was found on, counted from the
    TOP of the capture. It exists for the freeze comparison in the rule: a
    banner that MOVED between two captures means the pane is still producing
    output, which is an agent working (or quoting the incident), never one
    parked behind a wall.
    """

    readable: bool
    limited: bool = False
    window: str = ""
    reset_at: datetime | None = None
    reset_text: str = ""
    line_index: int | None = None
    detail: str = ""


def resolve_clock_near(
    *, hour: int, minute: int, now: datetime, tz: timezone
) -> datetime:
    """The occurrence of ``hour:minute`` NEAREST to ``now`` — past or future.

    The provider prints a bare wall-clock time ("resets 8am") with no date,
    and the naive readings are both wrong. "The next 8am" turns a wall that
    lifted an hour ago into a 23-hour wait; "today's 8am" is simply wrong
    either side of midnight.

    Nearest-occurrence is right because a reset is always near: a session
    window is hours and a weekly window still names a time inside the coming
    week. Half a day is the furthest an unlabelled clock time can honestly be
    from the moment you read it, so the candidate within ±12h is unique and
    is the one the provider meant.

    Worked, with the 2026-08-28 incident's own numbers: the banner said
    ``resets 7:10pm`` and was read at 21:00 UTC. Today's 19:10 is 1h50m
    behind; tomorrow's is 22h ahead. Nearest is today's — the wall is DOWN,
    which is the answer that gets the agent working again.
    """
    anchor = now.astimezone(tz)
    same_day = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidates = [same_day - timedelta(days=1), same_day, same_day + timedelta(days=1)]
    return min(candidates, key=lambda c: abs(c - anchor))


def _zone(name: str) -> timezone | None:
    """The tzinfo for a printed zone label, or ``None`` if we cannot name it.

    ``None`` is a real answer and the caller must not paper over it: a label
    we cannot resolve makes the printed clock time unanchored, and an
    unanchored reset is exactly the thing this module refuses to guess.
    """
    label = name.strip()
    if label.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    # stx-allow: fallback (reason: zoneinfo is stdlib but its tz DATABASE is an
    # OS package that can be absent in a minimal container; an unresolvable
    # label must degrade to "we cannot anchor this", never to a wrong instant)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(label)  # type: ignore[return-value]
    except Exception:
        return None


def parse_reset_at(
    text: str, *, now: datetime, default_tz: timezone
) -> tuple[datetime | None, str]:
    """Extract the reset instant from a banner line. ``(instant, raw)``.

    ``instant is None`` means the clause was absent or unparseable, and the
    ``raw`` half still carries whatever was matched so the operator sees WHAT
    could not be read rather than a bare failure.

    ``default_tz`` is used ONLY when the provider printed no zone label. That
    is a substitution, so it is the caller's frame — the zone the sweep is
    running in — and not a constant hidden in here.
    """
    match = _RESET_RE.search(text)
    if match is None:
        return None, ""
    raw = match.group(0).strip()
    clock = match.group("clock").strip()
    label = match.group("tz")
    tz = _zone(label) if label else default_tz
    if tz is None:
        return None, raw

    iso = _ISO_RE.match(clock)
    if iso is not None:
        year, month, day, hour, minute, second, zulu = iso.groups()
        return (
            datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
                tzinfo=timezone.utc if zulu else tz,
            ),
            raw,
        )

    bare = _CLOCK_RE.match(clock)
    if bare is None:
        return None, raw
    hour_s, minute_s, meridiem = bare.groups()
    hour = int(hour_s)
    minute = int(minute_s or 0)
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, raw
    return resolve_clock_near(hour=hour, minute=minute, now=now, tz=tz), raw


def observe_pane(
    pane: str | None, *, now: datetime, default_tz: timezone
) -> LimitObservation:
    """Read ONE captured pane for a rate wall. Pure.

    Scans from the BOTTOM up and reports the LAST banner, because a pane is a
    scrolling log: an older wall that has already lifted sits above a newer
    one, and the newest line is the only one describing the agent's current
    state.

    Left TUI decoration is stripped with the SAME stripper the auth matcher
    uses (:func:`.._runners._tmux.auth_status._strip_markers`), so the NBSP
    that Claude's Ink TUI renders after ``⎿`` is handled in one place. That
    NBSP is not a detail: leaving it out of the strip set once already made a
    real banner undetectable.
    """
    if pane is None:
        return LimitObservation(
            readable=False,
            detail="pane could not be captured — NO evidence, which is not "
            "evidence of a working agent",
        )

    from .._runners._tmux.auth_status import _strip_markers

    lines = pane.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = _strip_markers(lines[index])
        found = LIMIT_RE.search(stripped)
        if found is None:
            continue
        reset_at, raw = parse_reset_at(stripped, now=now, default_tz=default_tz)
        window = found.group("window").lower()
        if reset_at is None:
            unread = raw or "no reset clause at all"
            detail = (
                f"a {window} rate wall is rendered at pane line {index}, but its "
                f"reset clause could not be read ({unread!r}). Refusing to GUESS "
                f"when it lifts — a guessed reset is how a reviver starts "
                f"hammering a wall that is still up"
            )
        else:
            detail = (
                f"a {window} rate wall is rendered at pane line {index}, lifting "
                f"at {reset_at.isoformat()} (read from {raw!r})"
            )
        return LimitObservation(
            readable=True,
            limited=True,
            window=window,
            reset_at=reset_at,
            reset_text=raw,
            line_index=index,
            detail=detail,
        )
    return LimitObservation(
        readable=True,
        limited=False,
        detail="no rate-wall banner anywhere in the captured pane",
    )
