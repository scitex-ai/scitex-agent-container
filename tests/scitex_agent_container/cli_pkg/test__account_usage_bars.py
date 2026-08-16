"""Tests for the ``sac accounts list`` usage-bars + fleet-capacity surface.

PA-306 no-mocks: every test drives the real pure helpers with known
inputs and asserts exact strings / computed aggregates. The
bar-rendering and fleet-capacity functions take no I/O and an
injectable ``now``, so no monkeypatching of the clock or filesystem is
needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container.cli_pkg._account_list_render import AccountRow
from scitex_agent_container.cli_pkg._account_usage_bars import (
    fleet_7d_capacity_used,
    fleet_capacity_used_line,
    render_account_block,
    render_usage_bar,
    render_usage_bars_block,
    render_window_line,
)

# ---------------------------------------------------------------------------
# render_usage_bar — exact bar strings for known percentages
# ---------------------------------------------------------------------------


def test_render_usage_bar_zero_is_all_empty():
    # Arrange
    width = 20
    # Act
    bar = render_usage_bar(0.0, width=width)
    # Assert
    assert bar == "[" + "░" * 20 + "]"


def test_render_usage_bar_full_is_all_filled():
    # Arrange
    width = 20
    # Act
    bar = render_usage_bar(100.0, width=width)
    # Assert
    assert bar == "[" + "█" * 20 + "]"


def test_render_usage_bar_half_is_ten_and_ten():
    # Arrange — 50% of 20 cells = 10 filled.
    width = 20
    # Act
    bar = render_usage_bar(50.0, width=width)
    # Assert
    assert bar == "[" + "█" * 10 + "░" * 10 + "]"


def test_render_usage_bar_52_percent_width16_matches_example():
    # Arrange — the task's worked example: 52% of 16 cells → 8 filled.
    width = 16
    # Act
    bar = render_usage_bar(52.0, width=width)
    # Assert
    assert bar == "[████████░░░░░░░░]"


def test_render_usage_bar_99_never_shows_full_bar():
    # Arrange — 99% must stay visibly distinct from 100%.
    width = 20
    # Act
    bar = render_usage_bar(99.0, width=width)
    # Assert — exactly one empty cell.
    assert bar.count("░") == 1 and bar.count("█") == 19


def test_render_usage_bar_1_never_shows_empty_bar():
    # Arrange — 1% must stay visibly distinct from 0%.
    width = 20
    # Act
    bar = render_usage_bar(1.0, width=width)
    # Assert — exactly one filled cell.
    assert bar.count("█") == 1 and bar.count("░") == 19


def test_render_usage_bar_none_carries_no_data_text():
    # Arrange
    width = 20
    # Act
    bar = render_usage_bar(None, width=width)
    # Assert
    assert bar == "[" + "no data".center(20) + "]"


def test_render_usage_bar_none_same_width_as_real_bar():
    # Arrange — alignment contract: placeholder and real bar share width.
    real = render_usage_bar(50.0, width=20)
    # Act
    placeholder = render_usage_bar(None, width=20)
    # Assert
    assert len(real) == len(placeholder)


def test_render_usage_bar_clamps_over_100():
    # Arrange — >100 clamps to a full bar (no overflow cells).
    width = 20
    # Act
    bar = render_usage_bar(150.0, width=width)
    # Assert
    assert bar == "[" + "█" * 20 + "]"


# ---------------------------------------------------------------------------
# render_window_line / render_account_block — operator mockup 2026-07-17:
#   - <account>
#     5h (in 1h07m) [....................] (NN%)
#     7d (in 2d08h) [....................] (NN%)
# Reset hint BEFORE the bar (right after the window label); percent after
# the bar; hints padded to one block-level column so the bars align.
# ---------------------------------------------------------------------------


def test_render_window_line_hint_before_bar():
    # Arrange
    hint = "(in 4h05m)"
    # Act
    line = render_window_line("5h", 29.0, hint=hint, hint_width=10, width=20)
    # Assert — operator mockup: label, then hint, THEN the bar.
    assert line.startswith("  5h (in 4h05m) [")


def test_render_window_line_percent_after_bar():
    # Arrange
    hint = "(in 4h05m)"
    # Act
    line = render_window_line("5h", 29.0, hint=hint, hint_width=10, width=20)
    # Assert — operator mockup: the percent trails the bar, parenthesised.
    assert line.endswith("] (29%)")


def test_render_window_line_pads_missing_hint_to_column():
    # Arrange — a sibling row in the block has a 10-char hint.
    with_hint = render_window_line(
        "5h", 29.0, hint="(in 4h05m)", hint_width=10, width=20
    )
    # Act
    without_hint = render_window_line("5h", 14.0, hint="", hint_width=10, width=20)
    # Assert — the bar starts at the same column in both lines.
    assert with_hint.index("[") == without_hint.index("[")


def test_render_window_line_omits_hint_column_when_block_hintless():
    # Arrange — hint_width == 0: no row in the block has any cached reset.
    # Act
    line = render_window_line("5h", 14.0, hint="", hint_width=0, width=20)
    # Assert — no fabricated hint column; the bar follows the label.
    assert line.startswith("  5h [")


def test_render_window_line_unknown_pct_renders_unknown_label():
    # Arrange — the label was `(?)`, which reads as a typo or a rounding
    # artefact rather than as a statement. INCIDENT 2026-08-12: the operator
    # needs an absent measurement to announce itself in words.
    pct = None
    # Act
    line = render_window_line("7d", pct, hint="", hint_width=0, width=20)
    # Assert
    assert line.endswith("] (unknown)")


def test_render_account_block_first_line_is_dashed_name():
    # Arrange
    row = AccountRow(
        name="acct",
        freshness_state="VALID",
        freshness_hours=2.0,
        used_pct_5h=14.0,
        used_pct_7d=99.0,
        snapshot_as_of=None,
    )
    # Act
    block = render_account_block(row, hint_5h="", hint_7d="", hint_width=0, width=20)
    # Assert
    assert block[0] == "- claude-code:acct"


def test_render_account_block_is_5h_then_7d():
    # Arrange
    row = AccountRow(
        name="acct",
        freshness_state="VALID",
        freshness_hours=2.0,
        used_pct_5h=14.0,
        used_pct_7d=99.0,
        snapshot_as_of=None,
    )
    # Act
    block = render_account_block(row, hint_5h="", hint_7d="", hint_width=0, width=20)
    # Assert — one line per window, 5h first (operator mockup order).
    assert block[1].startswith("  5h ") and block[2].startswith("  7d ")


def test_render_usage_bars_block_empty_rows_is_empty_string():
    # Arrange
    rows: list[AccountRow] = []
    # Act
    block = render_usage_bars_block(rows)
    # Assert
    assert block == ""


def _two_bar_rows() -> list[AccountRow]:
    """Two accounts with different-length names for alignment tests.

    ``usage_state="known"`` is explicit because ``AccountRow`` defaults it to
    ``"unknown"`` — unknown-until-proven, so a row that never says it was
    measured does not get drawn as if it had been (INCIDENT 2026-08-12).
    These fixtures are asserting how a MEASURED reading renders, so they must
    declare that they are one.
    """
    return [
        AccountRow(
            name="alpha-example-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=0.0,
            used_pct_7d=99.0,
            snapshot_as_of=None,
            usage_state="known",
        ),
        AccountRow(
            name="beta-example-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=14.0,
            used_pct_7d=15.0,
            snapshot_as_of=None,
            usage_state="known",
        ),
    ]


def test_render_usage_bars_block_marks_each_account_with_dash():
    # Arrange
    rows = _two_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20)
    markers = [ln for ln in block.splitlines() if ln.startswith("- ")]
    # Assert — one "- <name>" marker per account, then the Average block
    # (operator 2026-07-30: the Average replaced the prose fleet line).
    assert markers == [
        "- claude-code:alpha-example-com",
        "- claude-code:beta-example-com",
        "- Average (n=2)",
    ]


def test_render_usage_bars_block_blank_line_between_accounts():
    # Arrange
    rows = _two_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20)
    # Assert — one separator between the two account blocks, plus one before
    # the Average block (operator 2026-07-30). The Average is separated the
    # same way an account is, so the section reads as a uniform list.
    assert block.splitlines().count("") == 2


def test_render_usage_bars_block_hintless_rows_share_bar_column():
    # Arrange — no row has a cached reset → no hint column at all.
    rows = _two_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20)
    starts = {ln.index("[") for ln in block.splitlines() if "[" in ln}
    # Assert — every bar starts at the same column (scans vertically).
    assert len(starts) == 1, f"bars misaligned: {starts}\n{block}"


def test_render_usage_bars_block_no_data_row_renders_placeholder():
    # Arrange — an account with no cached usage must not crash the block. It
    # now reads `unknown` rather than `no data`: an absent measurement and a
    # measurement sac cannot vouch for are the same thing to a reader
    # deciding whether to trust the row, and one word for it is enough.
    rows = [
        AccountRow(
            name="cold",
            freshness_state="ABSENT",
            freshness_hours=None,
            used_pct_5h=None,
            used_pct_7d=None,
            snapshot_as_of=None,
        ),
    ]
    # Act
    block = render_usage_bars_block(rows)
    # Assert
    assert "unknown" in block


# Fixed clock so the relative reset hints are deterministic (operator
# 2026-07-13: the hint is the time REMAINING until reset, not a wall-clock).
_HINT_NOW = datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc)


def _hinted_bar_rows() -> list[AccountRow]:
    """One row with both resets cached, one with neither (mixed block)."""
    return [
        AccountRow(
            name="alpha-example-com",
            freshness_state="VALID",
            freshness_hours=2.4,
            used_pct_5h=29.0,
            used_pct_7d=66.0,
            snapshot_as_of=None,
            # From _HINT_NOW: +4h05m → "in 4h05m"; +2d3h → "in 2d03h".
            reset_at_5h="2026-07-12T04:05:00+00:00",
            reset_at_7d="2026-07-14T03:00:00+00:00",
            usage_state="known",
        ),
        AccountRow(
            name="beta-example-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=14.0,
            used_pct_7d=15.0,
            snapshot_as_of=None,
            usage_state="known",
        ),
    ]


def test_render_usage_bars_block_5h_hint_precedes_bar():
    """Block-level: ``reset_at_5h`` renders ``5h (in 4h05m) [`` (hint first)."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    # Assert
    assert "5h (in 4h05m) [" in block


def test_render_usage_bars_block_7d_hint_precedes_bar():
    """Block-level: ``reset_at_7d`` renders ``7d (in 2d03h) [`` (hint first)."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    # Assert
    assert "7d (in 2d03h) [" in block


def test_render_usage_bars_block_aligns_bars_across_mixed_hints():
    """A hint-less row pads its hint slot — every bar starts at one column."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    starts = {ln.index("[") for ln in block.splitlines() if "[" in ln}
    # Assert — one distinct start column means the bars line up.
    assert len(starts) == 1, f"bars misaligned across rows: {starts}\n{block}"


def test_render_usage_bars_block_matches_operator_mockup_exactly():
    """The whole block, verbatim — the operator's 2026-07-17 layout spec."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=12, now=_HINT_NOW)
    # Assert — one account per block, hint before bar, percent after,
    # blank line between accounts, bars in one column.
    assert block == "\n".join(
        [
            "Usage bars (5h / 7d out of 100%):",
            "- claude-code:alpha-example-com",
            "  5h (in 4h05m) [███░░░░░░░░░] (29%)",
            "  7d (in 2d03h) [████████░░░░] (66%)",
            "",
            "- claude-code:beta-example-com",
            "  5h            [██░░░░░░░░░░] (14%)",
            "  7d            [██░░░░░░░░░░] (15%)",
            "",
            "- Average (n=2)",
            "  7d (in 2d03h) [█████░░░░░░░] (40%)",
        ]
    )


# ---------------------------------------------------------------------------
# fleet_7d_capacity_used — plain mean of the accounts' 7d utilisation
# (operator 2026-07-13: over the trailing 7 days, how much of the fleet's
# capacity was actually used — NOT reset-horizon-weighted).
# ---------------------------------------------------------------------------


def test_fleet_7d_capacity_counts_three_accounts():
    # Arrange — the operator's worked example: 7d = 17, 88, 88.
    values = [17.0, 88.0, 88.0]
    # Act
    _pct, n = fleet_7d_capacity_used(values)
    # Assert
    assert n == 3


def test_fleet_7d_capacity_mean_of_17_88_88_is_64():
    # Arrange — mean(17, 88, 88) = 64.33% → "64%" (NOT the old 15%).
    values = [17.0, 88.0, 88.0]
    # Act
    pct, _n = fleet_7d_capacity_used(values)
    # Assert
    assert int(round(pct)) == 64


def test_fleet_7d_capacity_skips_none_usage_accounts_count():
    # Arrange — an account with no usage data is excluded from the count.
    values = [100.0, None, 50.0]
    # Act
    _pct, n = fleet_7d_capacity_used(values)
    # Assert
    assert n == 2


def test_fleet_7d_capacity_skips_none_usage_accounts_mean():
    # Arrange — mean over the two accounts that DO have usage.
    values = [100.0, None, 50.0]
    # Act
    pct, _n = fleet_7d_capacity_used(values)
    # Assert
    assert pct == 75.0


def test_fleet_7d_capacity_all_none_returns_none():
    # Arrange
    values = [None, None]
    # Act
    pct, _n = fleet_7d_capacity_used(values)
    # Assert
    assert pct is None


def test_fleet_7d_capacity_all_none_counts_zero():
    # Arrange
    values = [None, None]
    # Act
    _pct, n = fleet_7d_capacity_used(values)
    # Assert
    assert n == 0


# ---------------------------------------------------------------------------
# fleet_capacity_used_line — CLI-facing formatting over AccountRow
# ---------------------------------------------------------------------------


def _row(name: str, d7: float | None) -> AccountRow:
    return AccountRow(
        name=name,
        freshness_state="VALID",
        freshness_hours=2.0,
        used_pct_5h=0.0,
        used_pct_7d=d7,
        snapshot_as_of=None,
    )


def test_fleet_capacity_line_operator_example_reads_64():
    # Arrange — the operator's worked fleet: 7d = 17, 88, 88 → 64% (not 15%).
    rows = [_row("a", 17.0), _row("b", 88.0), _row("c", 88.0)]
    # Act
    line = fleet_capacity_used_line(rows)
    # Assert
    assert line == "Fleet 7d capacity used: 64% (3 accounts)"


def test_fleet_capacity_line_singular_account_noun():
    # Arrange
    rows = [_row("solo", 42.0)]
    # Act
    line = fleet_capacity_used_line(rows)
    # Assert — singular "account", not "accounts".
    assert line == "Fleet 7d capacity used: 42% (1 account)"


def test_fleet_capacity_line_unavailable_when_no_usage():
    # Arrange
    rows = [_row("a", None), _row("b", None)]
    # Act
    line = fleet_capacity_used_line(rows)
    # Assert
    assert line == "Fleet 7d capacity used: unavailable (no usage data)"


def test_fleet_capacity_line_ignores_reset_horizon():
    # Arrange — an account at 100% whose 7d window resets in 24h. The OLD
    # reset-horizon weighting collapsed this to ~14%; the capacity figure
    # is the plain 7d% regardless of when the window rolls over.
    rows = [
        AccountRow(
            name="a",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=0.0,
            used_pct_7d=100.0,
            snapshot_as_of=None,
            reset_at_7d="2026-07-10T00:00:00+00:00",
        )
    ]
    # Act
    line = fleet_capacity_used_line(rows)
    # Assert — 100%, not the old horizon-weighted 14%.
    assert line == "Fleet 7d capacity used: 100% (1 account)"
