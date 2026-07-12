"""Pure formatting helpers for ``sac accounts list``.

Split out of ``_account_list_render.py`` to keep that file under the
per-file line cap once the per-row reset-hint renderers (gripe #2 of
the 2026-06-09 task) landed. Every helper here is pure (no I/O, no
clock, no environment side-effects beyond reading ``os.environ``) so
each can be unit-tested in isolation.

Concerns covered:

1. :func:`local_timezone` / :func:`format_dt_local` — render an
   ISO-8601 timestamp in the operator's local timezone. Precedence:
   ``SCITEX_AGENT_CONTAINER_TZ`` env wins, else ``TZ`` env, else the
   system local timezone (``datetime.astimezone()`` with no arg).

2. :func:`format_ttl_live` / :func:`format_snapshot_age` — render the
   credential TTL and the per-account usage snapshot age with enough
   resolution that a 60-second tick is visible under ``watch -n1``.

3. :func:`format_as_of_short` — render an As-of (Last-Update)
   timestamp as ``Sun 21h``.

The per-window reset hints on the usage-bars block used to live here
(``format_reset_hhmm`` / ``format_reset_day_hour``, absolute
``→HH:MM`` / ``→Day HHh``). The operator (2026-07-13) switched them to
the time REMAINING until reset (``in 4h05m`` / ``in 2d 3h``), rendered
by the shared :func:`~._timefmt.format_relative_until` — see
:mod:`._timefmt`, the SSOT for the JST wall clock and relative-duration
formatting that ``sac accounts list`` (and ``sac agents list``) share.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo

# ---------------------------------------------------------------------------
# Timezone resolution
# ---------------------------------------------------------------------------

# Project-specific env wins over the standard POSIX ``TZ`` so a host-wide
# ``TZ`` doesn't accidentally override an explicit per-tool preference.
_PROJECT_TZ_ENV = "SCITEX_AGENT_CONTAINER_TZ"


def local_timezone(env: dict[str, str] | None = None) -> tzinfo | None:
    """Return the effective render timezone for the operator.

    Precedence:

    1. ``SCITEX_AGENT_CONTAINER_TZ`` env — project-specific override.
    2. ``TZ`` env — standard POSIX.
    3. ``None`` — caller should pass ``None`` through to
       ``datetime.astimezone()`` which then picks up the system local
       timezone.

    Args:
        env: Override for ``os.environ`` (tests pass a dict).

    Returns:
        A ``tzinfo`` if one of the env vars resolves, else ``None``.
        Unknown / unparseable values silently fall through (we never
        want a typo in ``TZ`` to crash ``sac accounts list``).
    """
    src = env if env is not None else os.environ
    for key in (_PROJECT_TZ_ENV, "TZ"):
        name = src.get(key)
        if not name:
            continue
        tz = _resolve_tz(name)
        if tz is not None:
            return tz
    return None


def _resolve_tz(name: str) -> tzinfo | None:
    """Return a ``tzinfo`` for IANA ``name`` (``Asia/Tokyo``), or None.

    Uses the stdlib ``zoneinfo`` so no third-party dependency creeps in.
    Returns ``None`` on any failure so a bad env value falls through to
    the next layer of the precedence chain rather than crashing.
    """
    # stx-allow: fallback (reason: a bad TZ env value (typo, missing
    # tzdata on the host) must not crash `sac accounts list` — fall
    # through to the next precedence layer and ultimately system local.)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        return None
    except (
        Exception
    ):  # stx-allow: fallback (reason: catch-all safety net — see inline comment)
        return None


def format_dt_local(
    iso_or_dt: str | datetime | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Render an ISO-8601 timestamp (or aware datetime) in local TZ.

    Returns ``"-"`` for ``None`` / empty / unparseable input. Naive
    datetimes are assumed UTC (matches what the JSON path writes).
    """
    dt = _coerce_dt(iso_or_dt)
    if dt is None:
        return "-"
    tz = local_timezone(env)
    if tz is None:
        # No env override → system local (astimezone with no arg).
        return dt.astimezone().isoformat(timespec="seconds")
    return dt.astimezone(tz).isoformat(timespec="seconds")


def _coerce_dt(value: str | datetime | None) -> datetime | None:
    """Coerce ``value`` to an aware datetime; ``None`` on any failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    # stx-allow: fallback (reason: ISO parser is strict; a malformed
    # cache timestamp must render as "-" rather than crash the table.)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TTL + age formatting (must tick under watch -n1)
# ---------------------------------------------------------------------------


def format_ttl_live(hours: float | None) -> str:
    """Render signed-hours-to-expiry with minute-resolution.

    Prior format ``+2.8h`` collapsed a 60-second tick into the same
    string. This renders as ``+2h48m`` / ``-138h35m`` / ``+45s`` so a
    one-second tick under ``watch -n1`` is visible after ~60s.

    ``None`` → ``"-"``.
    """
    if hours is None:
        return "-"
    total_seconds = int(round(hours * 3600.0))
    sign = "+" if total_seconds >= 0 else "-"
    s = abs(total_seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{sign}{h}h{m:02d}m"
    if m:
        return f"{sign}{m}m{sec:02d}s"
    return f"{sign}{sec}s"


def format_snapshot_age(
    snapshot_iso: str | datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    """Render the per-account usage snapshot age as ``3m`` / ``1h`` / ``12s``.

    Used in the bullet-2 fix: the upstream usage% API is expensive to
    refetch on every render, so the snapshot is intentionally cached.
    Showing the age next to the % makes a stale number obvious instead
    of silently shipping yesterday's percentage as if it were live.

    ``None`` / unparseable → ``"?"``.
    """
    dt = _coerce_dt(snapshot_iso)
    if dt is None:
        return "?"
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    delta_s = int((now_dt - dt).total_seconds())
    if delta_s < 0:
        delta_s = 0
    if delta_s < 60:
        return f"{delta_s}s"
    if delta_s < 3600:
        return f"{delta_s // 60}m"
    if delta_s < 86400:
        return f"{delta_s // 3600}h"
    return f"{delta_s // 86400}d"


def format_as_of_short(
    iso_or_dt: str | datetime | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Render an As-of timestamp as day-of-week + hour: ``Sun 21h``.

    Uses the local-tz precedence chain (project env > TZ env > system
    local) so a UTC ``as_of`` lands in the operator's wall clock. The
    output is intentionally low-resolution — the operator only needs
    to know whether the value is from this hour, two hours ago, or
    yesterday. Sub-hour resolution is the snapshot AGE column's job
    (see :func:`format_snapshot_age`).

    ``None`` / unparseable → ``"-"``.
    """
    dt = _coerce_dt(iso_or_dt)
    if dt is None:
        return "-"
    tz = local_timezone(env)
    local = dt.astimezone(tz) if tz is not None else dt.astimezone()
    # %a = Sun/Mon/...; %H = 00-23.
    return local.strftime("%a %Hh")


__all__ = [
    "format_as_of_short",
    "format_dt_local",
    "format_snapshot_age",
    "format_ttl_live",
    "local_timezone",
]
