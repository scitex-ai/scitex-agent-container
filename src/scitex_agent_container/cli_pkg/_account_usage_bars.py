"""ASCII usage bars + fleet 7-day capacity figure for ``sac accounts list``.

Operator gripe (2026-07-09): "テーブルが見にくい" — the stored-accounts
rich table packs the 5h%/7d% numbers into terse cells that are hard to
scan across accounts. This module adds two purely-additive, purely-pure
(no I/O, no clock beyond an injectable ``now``) surfaces the CLI prints
BELOW the existing table:

1. A monospace **usage-bars block** — one 3-line BLOCK per account
   (operator mockup 2026-07-17, verbatim spec: 「usage bars のところ
   ちゃんとスペース取って書いてみたらどうでしょうか？」): the account
   name, then ONE line per window (5h, then 7d), a blank line between
   accounts. The one-line-per-account layout it replaces crammed both
   windows' bars + percentages + reset hints into ~120 chars and
   wrapped on a normal terminal. Since the 2026-07-11 dedupe directive
   the bars own the percentages; the per-window reset hint reads as the
   time REMAINING until the window resets (operator 2026-07-13 —
   relative, not a wall clock) and sits BEFORE the bar, right after the
   window label — deliberate (operator 2026-07-17): it foregrounds the
   time-to-reset the credential picker reasons about::

       Usage bars (5h / 7d out of 100%):
       - wyusuuke-gmail-com
         5h (in 4h05m) [██████░░░░░░░░░░░░░░] (29%)
         7d (in 2d03h) [█████████████░░░░░░░] (66%)

       - ywatanabe-scitex-ai
         5h            [███░░░░░░░░░░░░░░░░░] (14%)
         7d (in 5d00h) [███░░░░░░░░░░░░░░░░░] (15%)

   A bar for an account with no cached usage renders a same-width
   ``[      no data       ]`` placeholder rather than crashing; a
   window with no cached ``reset_at`` renders no hint, space-padded to
   the block-wide hint column so every bar (both windows, every
   account) starts at the same column and scans vertically. The
   relative hints are rendered by the shared
   :func:`~._timefmt.format_relative_until` (SSOT with the JST wall-clock
   helper used by the ``Since`` line).

2. A one-line **fleet 7-day capacity-used** figure — how much of the
   fleet's weekly capacity was actually consumed over the trailing 7
   days, a capacity-planning signal (≈100 % ⇒ saturated ⇒ add an
   account; low ⇒ over-provisioned ⇒ drop one)::

       Fleet 7d capacity used: 64% (3 accounts)

Fleet 7d capacity-used formula
------------------------------
The figure is the arithmetic **mean of the accounts' ``used_pct_7d``**
(the same 7-day-window utilisation the 7d bars show), over the accounts
that have a cached ``used_pct_7d``. Accounts with no usage data are
excluded from both the mean and the ``(N accounts)`` count; the figure
is ``None`` (CLI prints ``unavailable``) when no account has data.

Why the mean of the percentages (and not total-used / total-available)?
The Anthropic OAuth usage API returns a *percentage-utilisation* model
(``used_pct_7d`` ∈ [0, 100]) with NO absolute token quota — the raw
``limit_tokens_7d`` is ``None``. So total-used / total-available is not
computable from the available data, and the mean of the per-account
percentages is the only well-defined aggregate. When the accounts share
the same weekly quota (same plan tier) it equals total-used /
total-available exactly; if quotas differ it is a per-account-weighted
average, which still tracks the operator's add-/drop-an-account intent.

This REPLACES the earlier reset-horizon-weighted "effective utilization"
figure (removed 2026-07-13): weighting each account's 7d% by how soon
its window resets collapsed a fleet reading 17/88/88 (mean ≈ 64 %) down
to ~15 %, which the operator found misleading — a fleet at 88 % is
saturated regardless of when the window happens to roll over.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from ._timefmt import format_relative_until

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._account_list_render import AccountRow

# Bar glyphs — plain box-drawing blocks that render in any UTF-8 terminal
# (already used across the codebase's TUI/quota output).
_BAR_FILL = "█"
_BAR_EMPTY = "░"

_DEFAULT_BAR_WIDTH = 20


# ---------------------------------------------------------------------------
# Bar rendering (pure)
# ---------------------------------------------------------------------------


def render_usage_bar(pct: float | None, *, width: int = _DEFAULT_BAR_WIDTH) -> str:
    """Render ``pct`` (0-100) as a fixed-width bracketed ASCII bar.

    ``[████████░░░░░░░░░░░░]`` for a number; a same-width
    ``[      no data       ]`` placeholder for ``None`` so columns of
    bars stay vertically aligned whether or not a row has data.

    Guards against two visual lies:

    * a value ``< 100`` never renders a completely full bar (so 99 %
      is visibly distinct from 100 %);
    * a value ``> 0`` never renders a completely empty bar (so 1 % is
      visibly distinct from 0 %).

    Values outside ``[0, 100]`` are clamped. Never raises.
    """
    if width < 1:
        width = 1
    if pct is None:
        return "[" + "no data".center(width) + "]"
    p = max(0.0, min(100.0, float(pct)))
    filled = int(round(p / 100.0 * width))
    if filled >= width and p < 100.0:
        filled = width - 1
    if filled <= 0 and p > 0.0:
        filled = 1
    return "[" + _BAR_FILL * filled + _BAR_EMPTY * (width - filled) + "]"


def _pct_paren(pct: float | None) -> str:
    """Trailing percentage label: ``(29%)`` for a number, ``(?)`` for unknown."""
    if pct is None:
        return "(?)"
    return f"({int(round(max(0.0, min(100.0, float(pct)))))}%)"


def _wrap_hint(hint: str) -> str:
    """Parenthesise a non-empty reset hint: ``in 4h05m`` → ``(in 4h05m)``."""
    return f"({hint})" if hint else ""


def render_window_line(
    window: str,
    pct: float | None,
    *,
    hint: str,
    hint_width: int,
    width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """One per-window line of an account block, operator-mockup shape:

    ``  5h (in 4h05m) [██████░░░░░░░░░░░░░░] (29%)``

    The reset hint comes BEFORE the bar, right after the window label —
    deliberate (operator 2026-07-17): it foregrounds the time-to-reset
    the credential picker reasons about. ``hint`` is the pre-wrapped
    display string (``(in 4h05m)``) or ``""`` when the window has no
    cached reset; ``hint_width`` is the block-level max hint width and
    every line pads its hint to it, so the bars of BOTH windows of
    EVERY account start at the same column. ``hint_width == 0`` (no row
    in the block has any cached reset) omits the hint column entirely.
    """
    parts = [f"  {window}"]
    if hint_width > 0:
        parts.append(hint.ljust(max(hint_width, len(hint))))
    parts.append(render_usage_bar(pct, width=width))
    parts.append(_pct_paren(pct))
    return " ".join(parts)


def render_account_block(
    row: "AccountRow",
    *,
    hint_5h: str,
    hint_7d: str,
    hint_width: int,
    width: int = _DEFAULT_BAR_WIDTH,
) -> list[str]:
    """The 3-line block for one account (operator mockup 2026-07-17):

    ``- <name>`` then one :func:`render_window_line` per window
    (5h first, then 7d). The caller inserts the blank line BETWEEN
    accounts and supplies the block-level ``hint_width``.
    """
    return [
        f"- {row.name}",
        render_window_line(
            "5h", row.used_pct_5h, hint=hint_5h, hint_width=hint_width, width=width
        ),
        render_window_line(
            "7d", row.used_pct_7d, hint=hint_7d, hint_width=hint_width, width=width
        ),
    ]


def render_usage_bars_block(
    rows: Iterable["AccountRow"],
    *,
    width: int = _DEFAULT_BAR_WIDTH,
    now: datetime | None = None,
) -> str:
    """Render the full usage-bars block (header + one 3-line block per account).

    Operator mockup 2026-07-17 — one account per block, one line per
    window, a blank line between accounts (the single ~120-char line it
    replaces wrapped on a normal terminal). Each window line carries its
    compact reset hint (``(in 4h05m)`` / ``(in 2d03h)``) BEFORE the bar,
    computed from the row's ``reset_at_5h`` / ``reset_at_7d`` via the
    shared :func:`~._timefmt.format_relative_until` (operator
    2026-07-13: relative time-until-reset, not an absolute wall-clock).
    All hints share one block-level column width so every bar starts at
    the same column and the bars scan vertically.

    ``now`` is an injection seam for the current instant (tests pass a
    fixed value); it defaults to real wall-clock time.

    Returns ``""`` for an empty ``rows`` iterable so the caller can skip
    printing the section entirely.
    """
    row_list = list(rows)
    if not row_list:
        return ""
    hints_5h = [
        _wrap_hint(format_relative_until(r.reset_at_5h, now=now)) for r in row_list
    ]
    hints_7d = [
        _wrap_hint(format_relative_until(r.reset_at_7d, now=now)) for r in row_list
    ]
    hint_width = max(len(h) for h in (*hints_5h, *hints_7d))
    lines = ["Usage bars (5h / 7d out of 100%):"]
    for index, (r, hint_5h, hint_7d) in enumerate(zip(row_list, hints_5h, hints_7d)):
        if index:
            lines.append("")
        lines.extend(
            render_account_block(
                r,
                hint_5h=hint_5h,
                hint_7d=hint_7d,
                hint_width=hint_width,
                width=width,
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fleet 7-day capacity-used aggregate (pure)
# ---------------------------------------------------------------------------


def fleet_7d_capacity_used(
    values: Iterable[float | None],
) -> tuple[float | None, int]:
    """Mean 7-day utilisation across the accounts that have usage data.

    ``values`` is an iterable of per-account ``used_pct_7d`` (0-100) or
    ``None``. ``None`` entries (no cached usage) are excluded from BOTH
    the mean and the returned count. Returns ``(mean_pct, n_counted)``;
    the mean is ``None`` (and ``n`` is ``0``) when no account has data.

    See the module docstring for why the mean of the per-account
    percentages is the correct aggregate (the API exposes utilisation
    percentages, not absolute token quotas).
    """
    used = [float(v) for v in values if v is not None]
    if not used:
        return None, 0
    return sum(used) / len(used), len(used)


def fleet_capacity_used_line(rows: Iterable["AccountRow"]) -> str:
    """Format the fleet 7-day capacity-used line for the CLI.

    ``Fleet 7d capacity used: 64% (3 accounts)`` — the arithmetic mean
    of the accounts' 7-day-window utilisation (the same ``used_pct_7d``
    the 7d bars show) — or ``Fleet 7d capacity used: unavailable (no
    usage data)`` when no account has a cached 7-day utilisation.
    """
    pct, n = fleet_7d_capacity_used(r.used_pct_7d for r in rows)
    if pct is None:
        return "Fleet 7d capacity used: unavailable (no usage data)"
    noun = "account" if n == 1 else "accounts"
    return f"Fleet 7d capacity used: {int(round(pct))}% ({n} {noun})"


__all__ = [
    "fleet_7d_capacity_used",
    "fleet_capacity_used_line",
    "render_account_block",
    "render_usage_bar",
    "render_usage_bars_block",
    "render_window_line",
]
