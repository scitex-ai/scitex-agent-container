"""Where an agent has lived, and — the part that pays for itself — where a row came from.

The operator's idea (2026-08-07), and it has two payoffs that look unrelated
until you write them down:

  1. An AUDIT: which host was this agent on, and when. Useful on its own.
  2. The missing ATTRIBUTION signal. The cards `host` column is NULL on 3247 of
     3424 rows, so when two instances of one identity disagree, nothing can say
     which host wrote which row. A residency record makes that answerable after
     the fact, from a timestamp alone.

(2) is why this is worth building rather than nice to have. The 2026-08-07
split-brain was diagnosable only because someone happened to be watching; with
residency, "who wrote this" is a lookup.

THE INVARIANT THIS MODULE EXISTS TO HOLD: an agent lives in exactly ONE place at
a time. At most one residency is OPEN (``to_ts is None``), and opening a new one
CLOSES the previous in the same operation, so the "two homes at once" state is
unreachable rather than merely discouraged. That is the same shape as the write
lease — the illegal state cannot be expressed — applied to history rather than
to authority.

A record whose end precedes its start is refused where it is built, because a
history that can lie about time is worse than no history: it answers the
attribution question CONFIDENTLY and wrongly.

Pure: no clock, no storage, no I/O. ``now`` is passed in and a new immutable
tuple comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

__all__ = [
    "Residency",
    "current_host",
    "host_at",
    "open_residency",
]


@dataclass(frozen=True)
class Residency:
    """One stay: this agent lived on ``host`` from ``from_ts`` until ``to_ts``.

    ``to_ts is None`` means STILL LIVING THERE — an open interval, not a missing
    value. The two are worth distinguishing out loud, because a reader who takes
    None as "unknown end" will draw the opposite conclusion about the present.
    """

    host: str
    from_ts: float
    to_ts: float | None = None

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("Residency.host must be non-empty")
        if self.to_ts is not None and self.to_ts < self.from_ts:
            raise ValueError(
                f"Residency on {self.host!r} ends ({self.to_ts}) before it starts ({self.from_ts}) — "
                "a history that can lie about time answers attribution confidently and wrongly"
            )

    @property
    def is_open(self) -> bool:
        return self.to_ts is None

    def covers(self, when: float) -> bool:
        """True if the agent was on this host at ``when``.

        Half-open [from_ts, to_ts): the instant a stay ends belongs to the NEXT
        one, so a moment can never be claimed by two hosts. With relocation, the
        handover instant is exactly the moment two answers would be possible.
        """
        if when < self.from_ts:
            return False
        return self.to_ts is None or when < self.to_ts


def open_residency(
    history: tuple[Residency, ...],
    *,
    host: str,
    now: float,
) -> tuple[Residency, ...]:
    """Record a move to ``host``, closing any open stay in the same step.

    Idempotent on the host already open: re-recording a move that already
    happened returns the history unchanged rather than splitting one stay into
    two adjacent identical ones. A relocation coordinator re-running after a
    crash must not litter the record with the evidence of its own retries.

    Refuses to backdate: ``now`` before the open stay's start would make the
    closed interval end before it began, and the timestamps are the whole value
    of the record.
    """
    if not host:
        raise ValueError("cannot open a residency with no host")
    if not history:
        return (Residency(host=host, from_ts=now),)

    latest = history[-1]
    if latest.is_open:
        if latest.host == host:
            return history
        if now < latest.from_ts:
            raise ValueError(
                f"cannot move to {host!r} at {now}: the current stay on {latest.host!r} "
                f"began later, at {latest.from_ts}"
            )
        closed = replace(latest, to_ts=now)
        return history[:-1] + (closed, Residency(host=host, from_ts=now))

    if now < (latest.to_ts or latest.from_ts):
        raise ValueError(
            f"cannot open a stay on {host!r} at {now}: it precedes the end of the previous stay "
            f"({latest.to_ts})"
        )
    return history + (Residency(host=host, from_ts=now),)


def current_host(history: tuple[Residency, ...]) -> str | None:
    """The host the agent lives on now, or ``None`` if it lives nowhere.

    ``None`` is a real answer, not a gap: an agent whose last stay was closed
    and never reopened has been stopped, not misplaced.
    """
    if not history:
        return None
    latest = history[-1]
    return latest.host if latest.is_open else None


def host_at(history: tuple[Residency, ...], when: float) -> str | None:
    """Which host wrote a row stamped ``when`` — ``None`` if nothing covers it.

    This is the attribution lookup. ``None`` genuinely means the history does
    not know: before the first recorded stay, or inside a gap when the agent was
    stopped. It must not be read as "the current host" — that guess is exactly
    the kind that makes a split-brain look explained when it is not.
    """
    for stay in history:
        if stay.covers(when):
            return stay.host
    return None
