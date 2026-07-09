"""ASCII usage bars + fleet "effective utilization" for ``sac accounts list``.

Operator gripe (2026-07-09): "テーブルが見にくい" — the stored-accounts
rich table packs the 5h%/7d% numbers into terse cells that are hard to
scan across accounts. This module adds two purely-additive, purely-pure
(no I/O, no clock beyond an injectable ``now``) surfaces the CLI prints
BELOW the existing table:

1. A monospace **usage-bars block** — one fixed-width horizontal bar per
   window (5h short window + 7d window) per account, so utilisation is
   visible at a glance and the bars line up vertically across accounts::

       ywatanabe-scitex-ai   5h [░░░░░░░░░░░░░░░░░░░░]    0%   7d [████████████████████]  100%
       ywata1989-gmail-com   5h [███░░░░░░░░░░░░░░░░░░]   14%   7d [███░░░░░░░░░░░░░░░░░░]   15%

   A bar for an account with no cached usage renders a same-width
   ``[      no data       ]`` placeholder rather than crashing.

2. A one-line **fleet effective-utilization** figure that factors each
   account's 7-day-window reset horizon into a single fleet number:

       Fleet effective utilization: 71% (3 accounts)

Effective-utilization formula
-----------------------------
For each account, over a planning window ``W`` (default 168 h = 7 days,
matching the 7-day rolling window):

    frac_before_reset = clamp(reset_horizon_hours, 0, W) / W
    effective_util%   = frac_before_reset * used_pct_7d

Rationale: an account currently at ``used_pct_7d`` frees its whole
weekly allowance again once its 7-day window rolls over. So only the
portion of the planning window BEFORE that reset is "occupied" at the
current utilisation; after the reset the account is back near 0 % for
the rest of the window. An account at 100 % that resets in 1 day
(``frac ≈ 0.14`` → ``eff ≈ 14 %``) therefore contributes far more usable
weekly capacity than one at 100 % that resets in 6 days
(``frac ≈ 0.86`` → ``eff ≈ 86 %``).

The **reset horizon** is the true 7-day-window reset (``reset_at_7d``
from the Anthropic OAuth usage API, carried on
:class:`~._account_list_render.AccountRow`). When an account has no
``reset_at_7d`` cached (older cache / API outage), the horizon is
treated as the full window (``frac = 1`` → ``eff = used_pct_7d``): a
conservative "assume no reset within the window" default that never
understates utilisation.

The **fleet** figure is the arithmetic mean of the per-account
effective utilisations over the accounts that have a cached
``used_pct_7d`` (accounts with no usage data are excluded from the mean
and from the ``(N accounts)`` count). ``None`` when no account has
usage data — the CLI then prints ``unavailable`` instead of a number.

NB: this deliberately does NOT use the quota-cache ``ttl_h`` field —
that is the OAuth *access-token* TTL (typically ~2 h), not the 7-day
usage-window reset, so weighting by it would collapse every account's
effective utilisation to near-zero regardless of real usage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._account_list_render import AccountRow

# Planning window the effective-utilisation formula amortises over.
WEEK_HOURS: float = 7.0 * 24.0

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


def _pct_label(pct: float | None) -> str:
    """Right-justified 4-char percentage label: ``100%`` / ``  0%`` / ``   ?``."""
    if pct is None:
        return "   ?"
    return f"{int(round(max(0.0, min(100.0, float(pct)))))}%".rjust(4)


def render_usage_bar_line(
    label: str,
    pct_5h: float | None,
    pct_7d: float | None,
    *,
    label_width: int,
    width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """One aligned account line: ``<label>  5h [..] NN%   7d [..] NN%``."""
    bar5 = render_usage_bar(pct_5h, width=width)
    bar7 = render_usage_bar(pct_7d, width=width)
    return (
        f"  {label.ljust(label_width)}  "
        f"5h {bar5} {_pct_label(pct_5h)}   "
        f"7d {bar7} {_pct_label(pct_7d)}"
    )


def render_usage_bars_block(
    rows: Iterable["AccountRow"],
    *,
    width: int = _DEFAULT_BAR_WIDTH,
) -> str:
    """Render the full usage-bars block (header + one line per account).

    Returns ``""`` for an empty ``rows`` iterable so the caller can skip
    printing the section entirely.
    """
    row_list = list(rows)
    if not row_list:
        return ""
    label_width = max(len(r.name) for r in row_list)
    lines = ["Usage bars (5h / 7d out of 100%):"]
    for r in row_list:
        lines.append(
            render_usage_bar_line(
                r.name,
                r.used_pct_5h,
                r.used_pct_7d,
                label_width=label_width,
                width=width,
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Effective-utilisation formula (pure)
# ---------------------------------------------------------------------------


def effective_utilization_pct(
    used_pct_7d: float,
    reset_horizon_hours: float | None,
    *,
    window_hours: float = WEEK_HOURS,
) -> float:
    """Per-account effective utilisation weighted by the reset horizon.

    ``frac_before_reset = clamp(reset_horizon_hours, 0, window) / window``
    then ``effective = frac_before_reset * used_pct_7d``.

    ``reset_horizon_hours`` of ``None`` (no cached reset) means "assume
    no reset inside the window" → ``frac = 1`` → returns ``used_pct_7d``
    unchanged. See the module docstring for the full rationale.
    """
    if window_hours <= 0:
        return float(used_pct_7d)
    if reset_horizon_hours is None:
        frac = 1.0
    else:
        frac = max(0.0, min(float(reset_horizon_hours), window_hours)) / window_hours
    return frac * float(used_pct_7d)


def fleet_effective_utilization(
    pairs: Iterable[tuple[float | None, float | None]],
    *,
    window_hours: float = WEEK_HOURS,
) -> tuple[float | None, int]:
    """Aggregate effective utilisation across accounts.

    ``pairs`` is an iterable of ``(used_pct_7d, reset_horizon_hours)``.
    Entries whose ``used_pct_7d`` is ``None`` are excluded (no data).
    Returns ``(mean_effective_pct, n_accounts_counted)``; the mean is
    ``None`` (and ``n`` is ``0``) when no entry has usage data.
    """
    effs: list[float] = []
    for used_pct_7d, horizon in pairs:
        if used_pct_7d is None:
            continue
        effs.append(
            effective_utilization_pct(
                used_pct_7d, horizon, window_hours=window_hours
            )
        )
    if not effs:
        return None, 0
    return sum(effs) / len(effs), len(effs)


# ---------------------------------------------------------------------------
# AccountRow adapters (glue the pure formula to the CLI's row model)
# ---------------------------------------------------------------------------


def _reset_horizon_hours(
    reset_at_iso: str | None, *, now: datetime | None = None
) -> float | None:
    """Hours from ``now`` until ``reset_at_iso``; ``None`` if unparseable/absent.

    Past resets clamp to ``0.0`` (the window is due to roll over
    imminently). Naive timestamps are treated as UTC. Never raises.
    """
    if not reset_at_iso:
        return None
    # stx-allow: fallback (reason: a malformed cached reset timestamp must
    # degrade to "no horizon" rather than crash `sac accounts list`.)
    try:
        dt = datetime.fromisoformat(reset_at_iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    hours = (dt - now_dt).total_seconds() / 3600.0
    return max(0.0, hours)


def fleet_effective_line(
    rows: Iterable["AccountRow"], *, now: datetime | None = None
) -> str:
    """Format the fleet effective-utilisation line for the CLI.

    ``Fleet effective utilization: 71% (3 accounts)`` — or
    ``Fleet effective utilization: unavailable (no usage data)`` when
    no account has a cached 7-day utilisation.
    """
    pairs = [
        (r.used_pct_7d, _reset_horizon_hours(r.reset_at_7d, now=now))
        for r in rows
    ]
    pct, n = fleet_effective_utilization(pairs)
    if pct is None:
        return "Fleet effective utilization: unavailable (no usage data)"
    noun = "account" if n == 1 else "accounts"
    return f"Fleet effective utilization: {int(round(pct))}% ({n} {noun})"


__all__ = [
    "WEEK_HOURS",
    "effective_utilization_pct",
    "fleet_effective_line",
    "fleet_effective_utilization",
    "render_usage_bar",
    "render_usage_bar_line",
    "render_usage_bars_block",
]
