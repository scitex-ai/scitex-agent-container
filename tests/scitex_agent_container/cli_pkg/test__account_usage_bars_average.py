"""The ``- Average (n=N)`` block in the usage-bars section.

Operator request 2026-07-30: drop the prose ``Fleet 7d capacity used: 52%
(3 accounts)`` line and render the same arithmetic mean as a bar, in the
visual language of the rest of the section.

The number MUST stay identical to what the dropped line reported — this is a
re-rendering, not a new statistic — so one test pins the Average percentage
against ``fleet_7d_capacity_used`` directly rather than against a literal.

PA-306: no mocks — real AccountRow-shaped objects, real renderer.
AAA markers (TQ002); descriptive names; one assertion each (TQ007).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scitex_agent_container.cli_pkg._account_usage_bars import (
    fleet_7d_capacity_used,
    render_average_block,
    render_usage_bars_block,
)

_NOW = datetime(2026, 7, 30, 2, 52, tzinfo=timezone.utc)


def _row(name, pct_5h, pct_7d, *, in_5h=1.0, in_7d=100.0):
    return SimpleNamespace(
        provider="claude-code",
        name=name,
        used_pct_5h=pct_5h,
        used_pct_7d=pct_7d,
        reset_at_5h=_NOW + timedelta(hours=in_5h),
        reset_at_7d=_NOW + timedelta(hours=in_7d),
    )


def _rows():
    # The operator's real 2026-07-30 readings.
    return [
        _row("wyusuuke-gmail-com", 16, 64, in_5h=0.95, in_7d=91),
        _row("ywata1989-gmail-com", 13, 41, in_5h=3.95, in_7d=134),
        _row("ywatanabe-scitex-ai", 4, 52, in_5h=3.6, in_7d=125),
    ]


def test_the_average_header_carries_the_counted_population() -> None:
    # Arrange
    rows = _rows()

    # Act
    block = render_average_block(rows, hint_width=12, now=_NOW)

    # Assert
    assert block[0] == "- Average (n=3)"


def test_the_average_percentage_equals_the_dropped_fleet_number() -> None:
    # Arrange — this is a re-rendering, not a new statistic. Pinning against a
    # literal would let the two drift apart silently; pinning against the
    # aggregate function makes that impossible.
    rows = _rows()
    expected_pct, _ = fleet_7d_capacity_used(r.used_pct_7d for r in rows)

    # Act
    line = render_average_block(rows, hint_width=12, now=_NOW)[1]

    # Assert
    assert f"({int(round(expected_pct))}%)" in line


def test_the_average_block_renders_only_the_7d_window() -> None:
    # Arrange — 5h windows reset on staggered anchors; their mean is not
    # actionable, so the block deliberately carries a single window line.
    rows = _rows()

    # Act
    block = render_average_block(rows, hint_width=12, now=_NOW)

    # Assert
    assert len(block) == 2 and block[1].strip().startswith("7d")


def test_the_average_reset_hint_is_the_mean_of_the_rows_resets() -> None:
    # Arrange — resets at 91h, 134h and 125h, so the mean is 116.67h = 4d20h.
    rows = _rows()

    # Act
    line = render_average_block(rows, hint_width=12, now=_NOW)[1]

    # Assert
    assert "(in 4d20h)" in line


def test_an_account_without_7d_usage_is_excluded_from_the_count() -> None:
    # Arrange — n must describe the same population the mean averages over, or
    # the percentage and the count describe different things.
    rows = _rows() + [_row("no-data", None, None)]

    # Act
    block = render_average_block(rows, hint_width=12, now=_NOW)

    # Assert
    assert block[0] == "- Average (n=3)"


def test_no_average_block_when_no_account_has_usage_data() -> None:
    # Arrange — an empty bar would read as 0%, which is a claim, not an absence.
    rows = [_row("a", None, None), _row("b", None, None)]

    # Act
    block = render_average_block(rows, hint_width=12, now=_NOW)

    # Assert
    assert block == []


def test_the_average_appears_in_the_full_bars_block() -> None:
    # Arrange
    rows = _rows()

    # Act
    out = render_usage_bars_block(rows, now=_NOW)

    # Assert
    assert "- Average (n=3)" in out


def test_the_average_bar_starts_at_the_same_column_as_the_account_bars() -> None:
    # Arrange — the whole point of the section is a vertical scan. If the
    # Average hint were excluded from hint_width its bar would sit left of the
    # others. Regression guard for exactly that.
    out = render_usage_bars_block(_rows(), now=_NOW)

    # Act
    cols = {line.index("[") for line in out.splitlines() if "[" in line}

    # Assert
    assert len(cols) == 1


def test_the_average_is_last_so_the_per_account_order_is_unchanged() -> None:
    # Arrange
    out = render_usage_bars_block(_rows(), now=_NOW)

    # Act
    headers = [ln for ln in out.splitlines() if ln.startswith("- ")]

    # Assert
    assert headers[-1] == "- Average (n=3)"
