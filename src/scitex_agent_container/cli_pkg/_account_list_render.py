"""Renderer for ``sac accounts list`` — Stored-accounts table + helpers.

Split out of ``account_group.py`` to keep that file under the per-file
line cap (same pattern as ``_account_status.py`` and ``_account_refresh.py``)
and so the renderer is unit-testable without going through ``CliRunner``.

Three concerns live here, one per public helper:

1. :func:`local_timezone` / :func:`format_dt_local` — render an ISO-8601
   timestamp in the operator's local timezone. Precedence:
   ``SCITEX_AGENT_CONTAINER_TZ`` env wins, else ``TZ`` env, else the
   system local timezone (``datetime.astimezone()`` with no arg). This
   is the bullet-1 fix: the prior renderer emitted UTC.

2. :func:`format_ttl_live` / :func:`format_snapshot_age` — render the
   credential TTL and the per-account usage snapshot age with enough
   resolution that a 60-second tick is VISIBLE under ``watch -n1``. The
   prior ``+2.8h`` collapsed any sub-hour change into the same string,
   making the operator think the value was cached. TTL is computed
   live from ``expiresAt`` on every call; this module only formats it.

3. :func:`render_stored_table` — render the Stored-accounts block as a
   ``rich.table.Table`` with aligned columns:

       ID | Email | Plan | Status(+TTL) | 5h% | 7d% | As-of

   ``As-of`` is the per-account usage snapshot age in short day+hour
   form (``Sun 21h``), not microsecond ISO.

The JSON-output path of ``sac accounts list --json`` is NOT touched —
it still emits ISO-8601 ``as_of`` strings. Only the human renderer
reformats at display time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Timezone resolution (bullet 1)
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
# TTL + age formatting (bullet 2 — must tick under watch -n1)
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
    Showing the age next to the % makes a stale number OBVIOUS instead
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


# ---------------------------------------------------------------------------
# Row data model + table renderer (bullet 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountRow:
    """One row's worth of pre-resolved display data.

    The dataclass keeps the renderer pure (no I/O, no time calls), so a
    test can hand-roll a row and assert the exact cells without
    monkeypatching the clock.

    Attributes
    ----------
    name
        Account ID (stored slug, e.g. ``ywatanabe-scitex-ai``).
    email
        Display email or ``(no email)`` placeholder.
    plan_label
        Human plan label (``Pro`` / ``Max 5x`` / ``Max 20x`` / ``?``).
    tier
        Rate-limit tier slug.
    freshness_state
        ``"VALID"`` / ``"EXPIRED"`` / ``"ABSENT"``.
    freshness_hours
        Signed hours to expiry, or ``None`` for ABSENT.
    used_pct_5h, used_pct_7d
        Float percentages or ``None``.
    snapshot_as_of
        ISO-8601 string from the usage cache, or ``None``.
    """

    name: str
    email: str
    plan_label: str
    tier: str
    freshness_state: str
    freshness_hours: float | None
    used_pct_5h: float | None
    used_pct_7d: float | None
    snapshot_as_of: str | None


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{float(value):.0f}%"


def _fmt_status(state: str, hours: float | None) -> str:
    if state == "ABSENT" or hours is None:
        return "ABSENT"
    return f"{state} {format_ttl_live(hours)}"


def _fmt_as_of_cell(snapshot_as_of: str | None, *, now: datetime | None = None) -> str:
    """Combine short day+hour with age suffix: ``Sun 21h (3m)`` / ``- (?)``."""
    if not snapshot_as_of:
        return "-"
    return f"{format_as_of_short(snapshot_as_of)} ({format_snapshot_age(snapshot_as_of, now=now)})"


def render_stored_table(
    rows: list[AccountRow],
    *,
    now: datetime | None = None,
) -> Table:
    """Build a ``rich.table.Table`` for the Stored-accounts block.

    Columns (left-to-right):
      ID | Email | Plan | Status(+TTL) | 5h% | 7d% | As-of

    ``now`` is an injection seam so the bullet-2 liveness tests can
    drive the snapshot-age cell deterministically without monkeypatching
    ``datetime.now``.
    """
    table = Table(title="Stored accounts", title_justify="left", show_lines=False)
    table.add_column("ID", style="bold")
    table.add_column("Email")
    table.add_column("Plan")
    table.add_column("Status(+TTL)")
    table.add_column("5h%", justify="right")
    table.add_column("7d%", justify="right")
    table.add_column("As-of")
    for r in rows:
        table.add_row(
            r.name,
            r.email,
            f"{r.plan_label} [{r.tier}]",
            _fmt_status(r.freshness_state, r.freshness_hours),
            _fmt_pct(r.used_pct_5h),
            _fmt_pct(r.used_pct_7d),
            _fmt_as_of_cell(r.snapshot_as_of, now=now),
        )
    return table


def render_stored_table_to_str(
    rows: list[AccountRow],
    *,
    now: datetime | None = None,
    width: int = 120,
) -> str:
    """Render the Stored-accounts table to a plain string.

    Used by the CLI tests to assert column alignment without coupling
    to terminal width. ``Console(record=True)`` captures the rendered
    output verbatim.
    """
    console = Console(record=True, width=width, file=open(os.devnull, "w"))
    try:
        console.print(render_stored_table(rows, now=now))
        return console.export_text()
    finally:
        console.file.close()


# ---------------------------------------------------------------------------
# Per-account usage fetch + JSON/human orchestrators
# ---------------------------------------------------------------------------


def _per_account_usage_cache_path(name: str):
    """Return the absolute path of the per-account ``usage.json`` cache."""
    from pathlib import Path

    from .._state.account_store import _store_path

    return _store_path(None, Path.home()) / name / "usage.json"


def usage_for_account(acct_meta: dict, *, refresh: bool = False) -> dict | None:
    """Live PER-ACCOUNT usage fetch (5-min cache); ``--refresh`` busts it.

    The snapshot lives at
    ``~/.scitex/agent-container/accounts/<name>/.credentials.json``
    (cascade-resolved via ``_store_path``); the fetch result is cached
    next to that file as ``usage.json`` so the same
    ``read_account_usage_cache`` reader sees the live value across
    invocations. Any failure (missing snapshot, expired token, network
    error) returns ``None`` → caller renders ``"-"`` for that row only;
    the rest of the list keeps rendering.

    When ``refresh`` is true the on-disk ``usage.json`` is removed
    before the fetch so the API is hit even when the cache is fresh —
    wiring for ``sac accounts list --refresh``.
    """
    from pathlib import Path

    from .._account.claude_usage import fetch_usage_for_credentials
    from .._state.account_store import _store_path, read_account_usage_cache

    name = acct_meta.get("name")
    if not name:
        return None
    store = _store_path(None, Path.home())
    creds_path = store / name / ".credentials.json"
    if not creds_path.is_file():
        return read_account_usage_cache(name)
    if refresh:
        cache_path = _per_account_usage_cache_path(name)
        # stx-allow: fallback (reason: best-effort cache bust; if the
        # file is already gone or locked, the next call still re-fetches
        # because the cache reader gracefully returns None.)
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass
    # stx-allow: fallback (reason: fetch_usage_for_credentials is documented never-raise, but defence-in-depth so one bad row never crashes `account list`)
    try:
        result = fetch_usage_for_credentials(creds_path)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return read_account_usage_cache(name)
    if result.get("error") or result.get("used_pct_5h") is None:
        cached = read_account_usage_cache(name)
        return cached if cached else None
    return result


def build_stored_rows(
    accounts: list[dict], *, refresh: bool = False
) -> list[AccountRow]:
    """Convert stored-account dicts into :class:`AccountRow` for the table.

    Pure orchestration: pulls plan/tier (offline), credential freshness
    (live recompute from ``expiresAt`` on every call), and usage% (cached
    or re-fetched depending on ``refresh``).
    """
    from .._account.creds_sync import account_freshness
    from .._state.account_store import read_account_plan

    rows: list[AccountRow] = []
    for acct in accounts:
        name = acct["name"]
        plan = read_account_plan(name)
        fresh = account_freshness(name)
        usage = usage_for_account(acct, refresh=refresh) or {}
        rows.append(
            AccountRow(
                name=name,
                email=acct.get("email_address") or "(no email)",
                plan_label=plan.get("plan_label") or "?",
                tier=plan.get("rate_limit_tier") or "?",
                freshness_state=fresh.state,
                freshness_hours=fresh.hours,
                used_pct_5h=usage.get("used_pct_5h"),
                used_pct_7d=usage.get("used_pct_7d"),
                snapshot_as_of=usage.get("as_of") or usage.get("fetched_at"),
            )
        )
    return rows


def build_stored_json(accounts: list[dict], *, refresh: bool = False) -> list[dict]:
    """Enrich stored-account dicts for ``sac accounts list --json``.

    Each entry carries OFFLINE plan/tier, credential FRESHNESS
    (``state`` + signed hours), and the per-account usage payload.
    Timestamps remain ISO-8601 for JSON consumers — only the human
    renderer reformats.
    """
    from .._account.creds_sync import account_freshness
    from .._state.account_store import read_account_plan

    stored: list[dict] = []
    for acct in accounts:
        entry = dict(acct)
        entry.update(read_account_plan(acct["name"]))
        fresh = account_freshness(acct["name"])
        entry["freshness"] = fresh.state
        entry["freshness_hours"] = fresh.hours
        entry["usage"] = usage_for_account(acct, refresh=refresh)
        stored.append(entry)
    return stored


__all__ = [
    "AccountRow",
    "build_stored_json",
    "build_stored_rows",
    "format_as_of_short",
    "format_dt_local",
    "format_snapshot_age",
    "format_ttl_live",
    "local_timezone",
    "render_stored_table",
    "render_stored_table_to_str",
    "usage_for_account",
]
