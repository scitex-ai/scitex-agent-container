"""Shared wall-clock time formatting for the sac CLI (single SSOT).

Two operator-facing time renderers that MORE THAN ONE CLI surface needs,
kept here so every surface consumes one implementation instead of
hand-rolling a private copy:

1. :func:`format_jst` — render an ISO-8601 timestamp (or ``datetime``)
   as a compact, human-readable Japan-time wall clock
   ``YYYY-MM-DD HH:MM (JST)``. The raw ``...T...Z`` ISO string the
   credential / usage APIs return is unreadable at a glance
   (``2025-05-30T19:59:34.010055Z``); this is what the ``Since`` line of
   ``sac accounts list`` — and the ``Started`` column of
   ``sac agents list`` — should show instead. The zone is fixed to
   ``Asia/Tokyo`` (the fleet operator's wall clock) and the ``(JST)``
   abbreviation is DERIVED from the resolved tzinfo (``%Z``), never
   hard-coded, so it stays self-consistent if the zone is ever changed.

2. :func:`format_relative_until` — render the time REMAINING until a
   future timestamp as ``in 4h05m`` / ``in 2d08h`` / ``now``. Used by
   the usage-bars block of ``sac accounts list`` for the per-window
   reset hints: the operator wants "how long until the window resets"
   (relative), not the absolute wall clock of the reset instant.

Both helpers are pure (no I/O; the only clock read is the INJECTABLE
``now`` on :func:`format_relative_until`) and never raise — a
malformed/absent timestamp degrades to the caller-supplied ``empty``
sentinel rather than crashing the CLI table it feeds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

# The fleet operator's wall clock. An IANA name (not a fixed offset) so
# the abbreviation (``JST``) is derived from the stdlib tz database via
# ``strftime("%Z")`` rather than hard-coded — see the module docstring.
_JST_IANA = "Asia/Tokyo"


def _jst_tzinfo() -> tzinfo:
    """Return the ``Asia/Tokyo`` tzinfo, degrading to a fixed +09:00.

    Uses the stdlib ``zoneinfo`` so no third-party dependency creeps in.
    If the host has no ``tzdata`` for the zone (a locale-stripped
    container), fall back to a NAMED fixed +09:00 offset whose ``%Z``
    still renders ``JST`` — so the caller's format string never has to
    hard-code the abbreviation on either path.
    """
    # stx-allow: fallback (reason: a tzdata-less host must still render a
    # readable JST wall clock rather than crash `sac accounts list`; the
    # named fixed-offset fallback keeps %Z == "JST".)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_JST_IANA)
    except (
        Exception
    ):  # stx-allow: fallback (reason: catch-all safety net — see inline comment)
        return timezone(timedelta(hours=9), "JST")


def _coerce_dt(value: str | datetime | None) -> datetime | None:
    """Coerce ``value`` to an aware datetime; ``None`` on any failure.

    Naive datetimes / ISO strings are assumed UTC (matches what the
    credential + usage JSON writers emit). A trailing ``Z`` is
    normalised to ``+00:00`` first so ``datetime.fromisoformat`` accepts
    the common ``...T...Z`` API shape on every supported Python.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # stx-allow: fallback (reason: the ISO parser is strict; a malformed
    # cached timestamp must render as the caller's empty sentinel rather
    # than crash the CLI surface it feeds.)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_jst(value: str | datetime | None, *, empty: str = "-") -> str:
    """Render ``value`` as ``YYYY-MM-DD HH:MM (JST)`` in Japan time.

    Args:
        value: ISO-8601 string (``2025-05-30T19:59:34.010055Z``), an
            aware/naive ``datetime``, or ``None``. Naive inputs are
            assumed UTC.
        empty: What to return for ``None`` / empty / unparseable input.

    Returns:
        ``"2025-05-31 04:59 (JST)"`` for a valid instant, else ``empty``.
        The ``(JST)`` abbreviation is derived from the resolved tzinfo
        (``%Z``), not hard-coded.
    """
    dt = _coerce_dt(value)
    if dt is None:
        return empty
    local = dt.astimezone(_jst_tzinfo())
    abbrev = local.strftime("%Z") or "JST"
    return f"{local.strftime('%Y-%m-%d %H:%M')} ({abbrev})"


def _humanize_until(seconds: int) -> str:
    """Render a positive second delta as ``in 2d08h`` / ``in 4h05m`` / ``in 7m``.

    Picks the two most-significant units so the hint stays compact:
    days+hours beyond a day, hours+minutes within a day — the lesser
    unit zero-padded (``2d08h`` / ``4h05m``, operator mockup 2026-07-17)
    so the hints stay fixed-width and the usage bars they precede align
    in a column — bare minutes under the hour. A non-positive delta
    (reset already due) renders ``now``.
    """
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"in {days}d{hours:02d}h"
    if hours:
        return f"in {hours}h{minutes:02d}m"
    if minutes:
        return f"in {minutes}m"
    return "in <1m"


def format_relative_until(
    value: str | datetime | None,
    *,
    now: datetime | None = None,
    empty: str = "",
) -> str:
    """Render the time remaining until ``value`` as ``in 4h05m`` / ``in 2d08h``.

    Args:
        value: A FUTURE ISO-8601 string / datetime (e.g. a rolling-window
            ``resets_at``), or ``None``.
        now: Injection seam for the current instant (tests pass a fixed
            value). Defaults to ``datetime.now(timezone.utc)``. A naive
            ``now`` is assumed UTC.
        empty: What to return for ``None`` / empty / unparseable input.

    Returns:
        ``"in 4h05m"`` (< 1 day), ``"in 2d08h"`` (>= 1 day), ``"in 7m"``
        (< 1 hour), ``"now"`` (already due), else ``empty``.
    """
    dt = _coerce_dt(value)
    if dt is None:
        return empty
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    delta = int((dt - now_dt).total_seconds())
    return _humanize_until(delta)


__all__ = [
    "format_jst",
    "format_relative_until",
]
