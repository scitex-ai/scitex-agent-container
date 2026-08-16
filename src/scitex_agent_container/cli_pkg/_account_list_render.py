"""Renderer for ``sac accounts list`` — Stored-accounts table + helpers.

Split out of ``account_group.py`` to keep that file under the per-file
line cap (same pattern as ``_account_status.py`` and ``_account_refresh.py``)
and so the renderer is unit-testable without going through ``CliRunner``.

This module now holds the THREE pieces that need on-disk state:

1. :class:`AccountRow` — pre-resolved row dataclass (pure data; the
   formatting helpers live in :mod:`._account_list_format`).
2. :func:`render_stored_table` — turn provider-aware rows into a
   ``rich.table.Table``.
3. :func:`usage_for_account`, :func:`build_stored_rows`,
   :func:`build_stored_json` — fetch / shape the data the CLI command
   feeds into the renderer or the JSON path.

Pure formatting helpers (``local_timezone``, ``format_dt_local``,
``format_ttl_live``, ``format_snapshot_age``, ``format_as_of_short``)
are re-exported from :mod:`._account_list_format` so existing callers
(and the historical test surface) keep importing them from this module
without churn. The per-window reset hints on the usage-bars block are
now the shared :func:`~._timefmt.format_relative_until` (relative
time-until-reset).

Layout note (operator directive 2026-07-11): the Stored-accounts table
and the usage-bars block below it used to DUPLICATE the 5h%/7d%
numbers. The rule now is "the bars own the percentages; the table
holds only what the bars cannot express" — so the table is exactly
``Provider | Account | Status | Last Update`` (provider is part of identity;
the Email column was dropped because IDs are
email-derived slugs, and the Plan column was dropped outright), while
the per-window reset hints (relative ``in Xh Ym`` / ``in Xd Yh``,
2026-06-09 gripe #2 + 2026-07-13 relative switch) moved from the
removed 5h%/7d% cells onto the usage-bars lines (see
:mod:`._account_usage_bars`). The JSON output path
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
        Account ID (stored slug, e.g. ``researcher-example-org``; the
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
    usage_state
        ``"known"`` / ``"stale"`` / ``"unknown"`` — sac's STANDING to
        assert the percentages, decided by
        :func:`._account_usage_state.classify_usage`. ``used_pct_*`` is
        ``None`` whenever this is ``"unknown"``, so an unattributable
        figure is not merely undrawn but unrepresentable.
    usage_age_seconds, usage_reason
        The snapshot's age, and — when the reading is not ``known`` — a
        one-line prose statement of why, shown under the bars.
    identity_state, verified_email
        Whether the credential in this account's directory was CHECKED to
        belong to this account (``verified`` / ``mismatch`` /
        ``unverified``) and the email it actually authenticates as. The
        store carries no identity claim inside the credential, so without
        this check the directory name is the only thing naming the
        account — see :mod:`.._account.account_verify`.
    duplicate_of
        Name of the earlier account this one resolves to the same
        Anthropic account as, or ``None``.
    """

    name: str
    freshness_state: str
    freshness_hours: float | None
    used_pct_5h: float | None
    used_pct_7d: float | None
    snapshot_as_of: str | None
    reset_at_5h: str | None = None
    reset_at_7d: str | None = None
    provider: str = "claude-code"
    # Defaults to UNKNOWN, not "known", on purpose. A field that defaults to
    # the confident value makes every forgetful caller assert something it
    # never checked — the defect class this whole change exists to remove.
    # Unknown-until-proven fails safe: the worst a caller who forgets can do
    # is under-claim.
    usage_state: str = "unknown"
    usage_age_seconds: int | None = None
    usage_reason: str | None = None
    identity_state: str = "unverified"
    verified_email: str | None = None
    duplicate_of: str | None = None
    # WHICH MACHINE this credential lives on. Empty on the single-host path,
    # which is why the Host column only appears once something fills it in.
    # A credential is a per-host FILE and is not on the sync rail, so the same
    # account is routinely VALID on one machine and EXPIRED on another —
    # measured 2026-08-14, when a restart on one host refused with "no healthy
    # stored account" while the identical three accounts were hours-fresh on
    # another. Without this field the fleet table cannot say which is which.
    host: str = ""


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------


def _fmt_status(state: str, hours: float | None) -> str:
    if state == "ABSENT":
        return "ABSENT"
    if hours is None:
        return state
    return f"{state} {format_ttl_live(hours)}"


def _fmt_last_update_cell(
    snapshot_as_of: str | None, *, now: datetime | None = None
) -> str:
    """Combine short day+hour with age suffix: ``Sun 21h (3m)`` / ``- (?)``.

    This is the age of the USAGE SNAPSHOT and of nothing else. The column
    was headed ``Last Update``, which named no particular fact and sat one
    cell away from the credential's ``VALID +7h06m``; a reader had no way
    to tell which of the two it timestamped. The header now says so —
    see :func:`render_stored_table`.
    """
    if not snapshot_as_of:
        return "-"
    return f"{format_as_of_short(snapshot_as_of)} ({format_snapshot_age(snapshot_as_of, now=now)})"


def _fmt_identity_cell(row: AccountRow) -> str:
    """State WHO this row's credential proved to be, or that nobody asked.

    Three outcomes, never collapsed into two:

    * ``verified`` — the email is shown plainly (or ``ok`` when the store
      made no claim to compare against).
    * ``mismatch`` — the directory label is wrong; the cell NAMES the
      account the credential really belongs to, because that is the fact
      an operator needs in order to act on it.
    * ``unverified`` — sac could not ask. Shown as ``unverified``, never
      as blank and never as the label, since displaying an unchecked
      label here is exactly how a wrong name passes for a right one.

    Deliberately contains no square brackets: these cells go through
    ``rich``, which would parse ``[...]`` as markup.
    """
    if row.duplicate_of:
        return f"DUPLICATE of {row.duplicate_of}"
    if row.identity_state == "mismatch":
        return f"MISMATCH -> {row.verified_email or 'another account'}"
    if row.identity_state == "verified":
        return row.verified_email or "ok"
    return "unverified"


def render_stored_table(
    rows: list[AccountRow],
    *,
    now: datetime | None = None,
) -> Table:
    """Build a ``rich.table.Table`` for the Stored-accounts block.

    Columns (left-to-right):
      Provider | Account | Status | Identity | Usage as of

    ``Identity`` says whether the credential in this account's directory
    was CHECKED to belong to this account, and names the real owner when
    it does not (INCIDENT 2026-08-12 — one Anthropic account occupied two
    directories and was reported as two accounts' worth of headroom).

    ``Usage as of`` was headed ``Last Update``. That name asserted nothing
    in particular while sitting beside the credential TTL, so a fresh-
    looking age there was read as vouching for the percentage beside it.
    The header now names the one fact the cell carries: when the USAGE
    snapshot was taken.

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
    # The Host column appears ONLY when a row carries a host — i.e. in the
    # fleet view. Adding it unconditionally would put a column of one repeated
    # name in front of every single-host listing, and a column that always says
    # the same thing teaches the eye to skip the place where the answer lives.
    with_host = any(r.host for r in rows)
    table = Table(title="Stored accounts", title_justify="left", show_lines=False)
    if with_host:
        table.add_column("Host", style="cyan")
    table.add_column("Provider")
    table.add_column("Account", style="bold")
    table.add_column("Status")
    table.add_column("Identity")
    table.add_column("Usage as of")
    for r in rows:
        cells = [
            r.provider,
            r.name,
            _fmt_status(r.freshness_state, r.freshness_hours),
            _fmt_identity_cell(r),
            _fmt_last_update_cell(r.snapshot_as_of, now=now),
        ]
        table.add_row(*([r.host or "—", *cells] if with_host else cells))
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
    percentages and their ``(in ...)`` reset hints since 2026-07-11).
    """
    return (
        "Legend: 5h = rolling 5-hour window; 7d = rolling 7-day window. "
        "(in XhYYm / in XdYYh after the window label is the time until "
        "the next reset.)"
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
# Re-exports — data acquisition now lives in ``_account_list_build``
# ---------------------------------------------------------------------------
# Imported at the BOTTOM, after ``AccountRow`` exists, because
# ``_account_list_build`` constructs it. Keeping the import here (rather than
# at the top) means this module is fully defined before the other one runs,
# so the pair is safe to import in either order.
from ._account_list_build import (  # noqa: E402
    build_openai_row,
    build_openai_rows,
    build_provider_accounts_json,
    build_stored_json,
    build_stored_rows,
    openai_account_name,
    usage_for_account,
    verify_stored_identities,
)

__all__ = [
    "AccountRow",
    "build_stored_json",
    "build_stored_rows",
    "build_openai_row",
    "build_openai_rows",
    "build_provider_accounts_json",
    "format_as_of_short",
    "format_dt_local",
    "format_snapshot_age",
    "format_ttl_live",
    "local_timezone",
    "needs_rolling_legend",
    "openai_account_name",
    "render_stored_table",
    "render_stored_table_to_str",
    "rolling_legend_line",
    "usage_for_account",
    "verify_stored_identities",
]
