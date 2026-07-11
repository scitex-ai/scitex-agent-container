"""Renderer for ``sac accounts list`` — Stored-accounts table + helpers.

Split out of ``account_group.py`` to keep that file under the per-file
line cap (same pattern as ``_account_status.py`` and ``_account_refresh.py``)
and so the renderer is unit-testable without going through ``CliRunner``.

This module now holds the THREE pieces that need on-disk state:

1. :class:`AccountRow` — pre-resolved row dataclass (pure data; the
   formatting helpers live in :mod:`._account_list_format`).
2. :func:`render_stored_table` — turn rows into a ``rich.table.Table``.
3. :func:`usage_for_account`, :func:`build_stored_rows`,
   :func:`build_stored_json` — fetch / shape the data the CLI command
   feeds into the renderer or the JSON path.

Pure formatting helpers (``local_timezone``, ``format_dt_local``,
``format_ttl_live``, ``format_snapshot_age``, ``format_as_of_short``,
``format_reset_hhmm``, ``format_reset_day_hour``) are re-exported from
:mod:`._account_list_format` so existing callers (and the historical
test surface) keep importing them from this module without churn.

Layout note (operator directive 2026-07-11): the Stored-accounts table
and the usage-bars block below it used to DUPLICATE the 5h%/7d%
numbers. The rule now is "the bars own the percentages; the table
holds only what the bars cannot express" — so the table is exactly
``Account | Status | Last Update`` (the ID column was renamed
``Account``; the Email column was dropped because IDs are
email-derived slugs, and the Plan column was dropped outright), while
the per-window reset hints (``→HH:MM`` / ``→Day HHh``, 2026-06-09
gripe #2) moved from the removed 5h%/7d% cells onto the usage-bars
lines (see :mod:`._account_usage_bars`). The JSON output path
(``sac accounts list --json``) is NOT touched — its schema (including
``email_address`` and ``plan_label``) stays the machine-readable
contract downstream consumers parse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from rich.console import Console
from rich.table import Table

from ._account_list_format import (
    format_as_of_short,
    format_dt_local,
    format_reset_day_hour,
    format_reset_hhmm,
    format_snapshot_age,
    format_ttl_live,
    local_timezone,
)

# ---------------------------------------------------------------------------
# Row data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountRow:
    """One row's worth of pre-resolved display data.

    The dataclass keeps the renderer pure (no I/O, no time calls), so a
    test can hand-roll a row and assert the exact cells without
    monkeypatching the clock. It feeds BOTH human surfaces of
    ``sac accounts list``: the Stored-accounts table (name + status +
    last update) and the usage-bars block (percentages + reset hints).
    The former ``email`` / ``plan_label`` / ``tier`` fields were
    dropped with the 2026-07-11 dedupe directive — neither surface
    renders them any more (the JSON path keeps them via
    :func:`build_stored_json`).

    Attributes
    ----------
    name
        Account ID (stored slug, e.g. ``ywatanabe-scitex-ai``; the
        slugs are email-derived, which is why the table needs no
        separate Email column).
    freshness_state
        ``"VALID"`` / ``"EXPIRED"`` / ``"ABSENT"``.
    freshness_hours
        Signed hours to expiry, or ``None`` for ABSENT.
    used_pct_5h, used_pct_7d
        Float percentages or ``None`` (rendered by the bars block).
    snapshot_as_of
        ISO-8601 string from the usage cache, or ``None``.
    reset_at_5h, reset_at_7d
        ISO-8601 reset timestamps from the Anthropic OAuth usage API
        (``resets_at`` field, parsed by :mod:`._account.claude_usage`).
        ``None`` when the API did not return them (older caches /
        outages); the bars block then omits the reset hint for that
        window rather than fabricating a value.
    """

    name: str
    freshness_state: str
    freshness_hours: float | None
    used_pct_5h: float | None
    used_pct_7d: float | None
    snapshot_as_of: str | None
    reset_at_5h: str | None = None
    reset_at_7d: str | None = None


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------


def _fmt_status(state: str, hours: float | None) -> str:
    if state == "ABSENT" or hours is None:
        return "ABSENT"
    return f"{state} {format_ttl_live(hours)}"


def _fmt_last_update_cell(
    snapshot_as_of: str | None, *, now: datetime | None = None
) -> str:
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
      Account | Status | Last Update

    Operator directive 2026-07-11: the table holds ONLY what the
    usage-bars block below it cannot express — the account slug, the
    credential status with its live token TTL (``VALID +2h26m``), and
    the usage-snapshot freshness. The 5h%/7d% columns (duplicating the
    bars), the Email column (IDs are email-derived slugs) and the Plan
    column were removed; the per-window reset hints moved onto the
    bars lines (:mod:`._account_usage_bars`).

    ``now`` is an injection seam so the snapshot-age tests can drive
    the Last-Update cell deterministically without monkeypatching
    ``datetime.now``.
    """
    table = Table(title="Stored accounts", title_justify="left", show_lines=False)
    table.add_column("Account", style="bold")
    table.add_column("Status")
    table.add_column("Last Update")
    for r in rows:
        table.add_row(
            r.name,
            _fmt_status(r.freshness_state, r.freshness_hours),
            _fmt_last_update_cell(r.snapshot_as_of, now=now),
        )
    return table


def needs_rolling_legend(rows: list[AccountRow]) -> bool:
    """Return True iff at least one row lacks BOTH reset_at fields.

    Used by the CLI to decide whether to print the explanatory legend
    below the usage-bars block. When EVERY row has a per-line reset
    hint, the legend would be redundant — the bars lines already carry
    the information.
    """
    if not rows:
        return False
    return any(r.reset_at_5h is None and r.reset_at_7d is None for r in rows)


def rolling_legend_line() -> str:
    """One-line legend operators see when reset_at is missing.

    Per the 2026-06-09 task contract: "リセットのアンカーが取れない
    場合は列凡例/ヘッダで5h=直近5時間ローリング, 7d=直近7日ローリン
    グと明示". Printed below the usage-bars block (which owns the
    percentages and their ``(→...)`` reset hints since 2026-07-11).
    """
    return (
        "Legend: 5h = rolling 5-hour window; 7d = rolling 7-day window. "
        "(→HH:MM / →Day HHh next to the % marks the next reset.)"
    )


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
    """Convert stored-account dicts into :class:`AccountRow` for rendering.

    Pure orchestration: pulls credential freshness (live recompute from
    ``expiresAt`` on every call) and usage% (cached or re-fetched
    depending on ``refresh``). Also carries through the per-window
    ``reset_at_5h`` / ``reset_at_7d`` so the usage-bars block can render
    the inline reset hint (gripe #2 of 2026-06-09; moved from the table
    cells onto the bars by the 2026-07-11 dedupe directive). Plan/tier
    are no longer resolved here — no human surface renders them (the
    JSON path keeps them via :func:`build_stored_json`).
    """
    from .._account.creds_sync import account_freshness

    rows: list[AccountRow] = []
    for acct in accounts:
        name = acct["name"]
        fresh = account_freshness(name)
        usage = usage_for_account(acct, refresh=refresh) or {}
        rows.append(
            AccountRow(
                name=name,
                freshness_state=fresh.state,
                freshness_hours=fresh.hours,
                used_pct_5h=usage.get("used_pct_5h"),
                used_pct_7d=usage.get("used_pct_7d"),
                snapshot_as_of=usage.get("as_of") or usage.get("fetched_at"),
                reset_at_5h=usage.get("reset_at_5h"),
                reset_at_7d=usage.get("reset_at_7d"),
            )
        )
    return rows


def build_stored_json(accounts: list[dict], *, refresh: bool = False) -> list[dict]:
    """Enrich stored-account dicts for ``sac accounts list --json``.

    Each entry carries OFFLINE plan/tier, credential FRESHNESS
    (``state`` + signed hours), and the per-account usage payload.
    Timestamps remain ISO-8601 for JSON consumers — only the human
    renderer reformats. The usage dict already carries
    ``reset_at_5h`` / ``reset_at_7d`` from the upstream API, so JSON
    consumers can compute their own reset hints if they want one.
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
    "format_reset_day_hour",
    "format_reset_hhmm",
    "format_snapshot_age",
    "format_ttl_live",
    "local_timezone",
    "needs_rolling_legend",
    "render_stored_table",
    "render_stored_table_to_str",
    "rolling_legend_line",
    "usage_for_account",
]
