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
    render_usage_bar,
    render_usage_bar_line,
    render_usage_bars_block,
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
# render_usage_bar_line / block — alignment across accounts
# ---------------------------------------------------------------------------


def test_render_usage_bar_line_shows_5h_window():
    # Arrange
    label = "acct"
    # Act
    line = render_usage_bar_line(label, 14.0, 99.0, label_width=10, width=20)
    # Assert
    assert "5h [" in line and "14%" in line


def test_render_usage_bar_line_shows_7d_window():
    # Arrange
    label = "acct"
    # Act
    line = render_usage_bar_line(label, 14.0, 99.0, label_width=10, width=20)
    # Assert
    assert "7d [" in line and "99%" in line


# ---------------------------------------------------------------------------
# 2026-07-11 dedupe directive — the bars own the reset hints (moved off the
# Stored-accounts table). Operator's verbatim example shape:
#   ... 5h [..]  29% (in 4h05m)   7d [..]  66% (in 2d 3h)
# ---------------------------------------------------------------------------


def test_render_usage_bar_line_appends_5h_reset_hint():
    # Arrange — pre-wrapped hints as the block passes them.
    label = "acct"
    # Act
    line = render_usage_bar_line(
        label,
        29.0,
        66.0,
        label_width=10,
        width=20,
        hint_5h="(in 4h05m)",
        hint_7d="(in 2d 3h)",
    )
    # Assert — operator's verbatim 5h shape.
    assert "29% (in 4h05m)" in line


def test_render_usage_bar_line_appends_7d_reset_hint():
    # Arrange
    label = "acct"
    # Act
    line = render_usage_bar_line(
        label,
        29.0,
        66.0,
        label_width=10,
        width=20,
        hint_5h="(in 4h05m)",
        hint_7d="(in 2d 3h)",
    )
    # Assert — operator's verbatim 7d shape.
    assert "66% (in 2d 3h)" in line


def test_render_usage_bar_line_without_hints_has_no_parens():
    """No cached reset → no fabricated hint; the line stays hint-free."""
    # Arrange
    label = "acct"
    # Act
    line = render_usage_bar_line(label, 14.0, 99.0, label_width=10, width=20)
    # Assert
    assert "(" not in line


def test_render_usage_bar_line_pads_missing_5h_hint_to_block_width():
    """A hint-less row pads the 5h slot so the 7d bars stay aligned."""
    # Arrange — a sibling row in the block has a 10-char 5h hint.
    with_hint = render_usage_bar_line(
        "aa", 29.0, 66.0, label_width=4, width=20, hint_5h="(in 4h05m)", hint_5h_width=10
    )
    # Act
    without_hint = render_usage_bar_line(
        "bbbb", 14.0, 15.0, label_width=4, width=20, hint_5h="", hint_5h_width=10
    )
    # Assert — "7d [" starts at the same column in both lines.
    assert with_hint.index("7d [") == without_hint.index("7d [")


def test_render_usage_bars_block_empty_rows_is_empty_string():
    # Arrange
    rows: list[AccountRow] = []
    # Act
    block = render_usage_bars_block(rows)
    # Assert
    assert block == ""


def _two_bar_rows() -> list[AccountRow]:
    """Two accounts with different-length names for alignment tests."""
    return [
        AccountRow(
            name="wyusuuke-gmail-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=0.0,
            used_pct_7d=99.0,
            snapshot_as_of=None,
        ),
        AccountRow(
            name="ywata1989-gmail-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=14.0,
            used_pct_7d=15.0,
            snapshot_as_of=None,
        ),
    ]


def test_render_usage_bars_block_lines_are_length_aligned():
    # Arrange
    rows = _two_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20)
    data_lines = [ln for ln in block.splitlines() if "5h [" in ln]
    # Assert — every data line has identical width (monospace aligned).
    assert len({len(ln) for ln in data_lines}) == 1


def test_render_usage_bars_block_no_data_row_renders_placeholder():
    # Arrange — an account with no cached usage must not crash the block.
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
    assert "no data" in block


# Fixed clock so the relative reset hints are deterministic (operator
# 2026-07-13: the hint is the time REMAINING until reset, not a wall-clock).
_HINT_NOW = datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc)


def _hinted_bar_rows() -> list[AccountRow]:
    """One row with both resets cached, one with neither (mixed block)."""
    return [
        AccountRow(
            name="wyusuuke-gmail-com",
            freshness_state="VALID",
            freshness_hours=2.4,
            used_pct_5h=29.0,
            used_pct_7d=66.0,
            snapshot_as_of=None,
            # From _HINT_NOW: +4h05m → "in 4h05m"; +2d3h → "in 2d 3h".
            reset_at_5h="2026-07-12T04:05:00+00:00",
            reset_at_7d="2026-07-14T03:00:00+00:00",
        ),
        AccountRow(
            name="ywata1989-gmail-com",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=14.0,
            used_pct_7d=15.0,
            snapshot_as_of=None,
        ),
    ]


def test_render_usage_bars_block_carries_5h_reset_hint():
    """Block-level: ``reset_at_5h`` renders the operator's ``29% (in 4h05m)``."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    # Assert
    assert "29% (in 4h05m)" in block


def test_render_usage_bars_block_carries_7d_reset_hint():
    """Block-level: ``reset_at_7d`` renders the operator's ``66% (in 2d 3h)``."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    # Assert
    assert "66% (in 2d 3h)" in block


def test_render_usage_bars_block_aligns_7d_bars_across_mixed_hints():
    """A hint-less row pads its 5h slot — 7d bars align across the block."""
    # Arrange
    rows = _hinted_bar_rows()
    # Act
    block = render_usage_bars_block(rows, width=20, now=_HINT_NOW)
    starts = {ln.index("7d [") for ln in block.splitlines() if "7d [" in ln}
    # Assert — one distinct start column means the 7d bars line up.
    assert len(starts) == 1, f"7d bars misaligned across rows: {starts}\n{block}"


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
