"""Tests for the ``sac accounts list`` usage-bars + fleet-effective surface.

PA-306 no-mocks: every test drives the real pure helpers with known
inputs and asserts exact strings / computed aggregates. The
bar-rendering and effective-utilization functions take no I/O and an
injectable ``now``, so no monkeypatching of the clock or filesystem is
needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container.cli_pkg._account_list_render import AccountRow
from scitex_agent_container.cli_pkg._account_usage_bars import (
    WEEK_HOURS,
    effective_utilization_pct,
    fleet_effective_line,
    fleet_effective_utilization,
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
            email="w@x",
            plan_label="Max 20x",
            tier="t",
            freshness_state="VALID",
            freshness_hours=2.0,
            used_pct_5h=0.0,
            used_pct_7d=99.0,
            snapshot_as_of=None,
        ),
        AccountRow(
            name="ywata1989-gmail-com",
            email="y@x",
            plan_label="Pro",
            tier="t",
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
            email="c@x",
            plan_label="?",
            tier="?",
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


# ---------------------------------------------------------------------------
# effective_utilization_pct — per-account reset-horizon weighting
# ---------------------------------------------------------------------------


def test_effective_util_none_horizon_returns_raw_pct():
    # Arrange — no reset horizon → assume no reset within the window.
    used = 100.0
    # Act
    eff = effective_utilization_pct(used, None)
    # Assert
    assert eff == 100.0


def test_effective_util_reset_in_one_day_at_100():
    # Arrange — 100% resetting in 24h over a 168h window → 24/168*100.
    used = 100.0
    # Act
    eff = effective_utilization_pct(used, 24.0, window_hours=WEEK_HOURS)
    # Assert
    assert round(eff, 4) == round(24.0 / 168.0 * 100.0, 4)


def test_effective_util_reset_in_six_days_higher_than_one_day():
    # Arrange — the rationale: later reset ⇒ higher effective util.
    one_day = effective_utilization_pct(100.0, 24.0)
    # Act
    six_days = effective_utilization_pct(100.0, 6 * 24.0)
    # Assert
    assert six_days > one_day


def test_effective_util_horizon_beyond_window_caps_at_raw():
    # Arrange — horizon > window clamps frac to 1.0 → raw pct.
    used = 80.0
    # Act
    eff = effective_utilization_pct(used, 1000.0, window_hours=WEEK_HOURS)
    # Assert
    assert eff == 80.0


def test_effective_util_past_reset_zero_horizon_is_zero():
    # Arrange — reset already due (horizon 0) → 0% effective utilisation.
    used = 100.0
    # Act
    eff = effective_utilization_pct(used, 0.0)
    # Assert
    assert eff == 0.0


# ---------------------------------------------------------------------------
# fleet_effective_utilization — aggregate
# ---------------------------------------------------------------------------


def test_fleet_effective_counts_three_accounts():
    # Arrange — the operator's real 3-account fleet: d7 = 99, 15, 100.
    pairs = [(99.0, None), (15.0, None), (100.0, None)]
    # Act
    _pct, n = fleet_effective_utilization(pairs)
    # Assert
    assert n == 3


def test_fleet_effective_mean_of_three_no_horizon_is_71():
    # Arrange — mean(99, 15, 100) = 71.33% → "71%".
    pairs = [(99.0, None), (15.0, None), (100.0, None)]
    # Act
    pct, _n = fleet_effective_utilization(pairs)
    # Assert
    assert int(round(pct)) == 71


def test_fleet_effective_skips_none_usage_accounts_count():
    # Arrange — an account with no usage data is excluded from the count.
    pairs = [(100.0, None), (None, None), (50.0, None)]
    # Act
    _pct, n = fleet_effective_utilization(pairs)
    # Assert
    assert n == 2


def test_fleet_effective_skips_none_usage_accounts_mean():
    # Arrange — mean over the two accounts that DO have usage.
    pairs = [(100.0, None), (None, None), (50.0, None)]
    # Act
    pct, _n = fleet_effective_utilization(pairs)
    # Assert
    assert pct == 75.0


def test_fleet_effective_all_none_returns_none():
    # Arrange
    pairs = [(None, None), (None, 24.0)]
    # Act
    pct, _n = fleet_effective_utilization(pairs)
    # Assert
    assert pct is None


def test_fleet_effective_all_none_counts_zero():
    # Arrange
    pairs = [(None, None), (None, 24.0)]
    # Act
    _pct, n = fleet_effective_utilization(pairs)
    # Assert
    assert n == 0


# ---------------------------------------------------------------------------
# fleet_effective_line — CLI-facing formatting over AccountRow
# ---------------------------------------------------------------------------


def _row(name: str, d7: float | None, reset_at_7d: str | None) -> AccountRow:
    return AccountRow(
        name=name,
        email=f"{name}@x",
        plan_label="Pro",
        tier="t",
        freshness_state="VALID",
        freshness_hours=2.0,
        used_pct_5h=0.0,
        used_pct_7d=d7,
        snapshot_as_of=None,
        reset_at_7d=reset_at_7d,
    )


def test_fleet_effective_line_three_accounts_no_reset():
    # Arrange — matches the operator's fleet; no reset_at → mean of d7.
    rows = [_row("a", 99.0, None), _row("b", 15.0, None), _row("c", 100.0, None)]
    # Act
    line = fleet_effective_line(rows)
    # Assert
    assert line == "Fleet effective utilization: 71% (3 accounts)"


def test_fleet_effective_line_singular_account_noun():
    # Arrange
    rows = [_row("solo", 42.0, None)]
    # Act
    line = fleet_effective_line(rows)
    # Assert — singular "account", not "accounts".
    assert line == "Fleet effective utilization: 42% (1 account)"


def test_fleet_effective_line_unavailable_when_no_usage():
    # Arrange
    rows = [_row("a", None, None), _row("b", None, None)]
    # Act
    line = fleet_effective_line(rows)
    # Assert
    assert line == "Fleet effective utilization: unavailable (no usage data)"


def test_fleet_effective_line_weights_by_reset_horizon():
    # Arrange — one account at 100% resetting in 24h over a 168h window.
    now = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    reset_in_24h = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    rows = [_row("a", 100.0, reset_in_24h)]
    # Act
    line = fleet_effective_line(rows, now=now)
    # Assert — 24/168*100 = 14.28 → "14%".
    assert line == "Fleet effective utilization: 14% (1 account)"
