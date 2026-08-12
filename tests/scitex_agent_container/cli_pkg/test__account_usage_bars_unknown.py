"""The bars must not draw what sac cannot vouch for (INCIDENT 2026-08-12).

A bar is an assertion. Before this change every reading got one, so a 2 %
belonging to another account looked exactly like a measured 2 %, and the
fleet Average counted it. These tests pin the visual contract: an unknown
reading produces NO fill glyphs at all, a stale one always carries its age,
and the Average states how many rows it declined to count.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._account_list_render import AccountRow
from scitex_agent_container.cli_pkg._account_usage_bars import (
    render_account_block,
    render_average_block,
    render_window_line,
)
from scitex_agent_container.cli_pkg._account_usage_state import KNOWN, STALE, UNKNOWN

_FILL = "█"


def _row(name, pct, state, **kw):
    return AccountRow(
        name=name,
        freshness_state="VALID",
        freshness_hours=7.0,
        used_pct_5h=pct,
        used_pct_7d=pct,
        snapshot_as_of=None,
        usage_state=state,
        **kw,
    )


# ---------------------------------------------------------------------------
# unknown draws nothing
# ---------------------------------------------------------------------------


def test_unknown_window_draws_no_filled_blocks():
    # Arrange — a percentage IS supplied; the state must override it.
    pct = 3.0
    # Act
    line = render_window_line("7d", pct, hint="", hint_width=0, state=UNKNOWN)
    # Assert
    assert _FILL not in line


def test_unknown_window_is_labelled_unknown():
    # Arrange
    pct = 3.0
    # Act
    line = render_window_line("7d", pct, hint="", hint_width=0, state=UNKNOWN)
    # Assert
    assert "(unknown)" in line


def test_unknown_window_says_unknown_inside_the_bar():
    # Arrange — "no data" would read as an absent account rather than an
    # unattributable one.
    pct = None
    # Act
    line = render_window_line("5h", pct, hint="", hint_width=0, state=UNKNOWN)
    # Assert
    assert "unknown" in line.split("]")[0]


# ---------------------------------------------------------------------------
# stale always shows its age
# ---------------------------------------------------------------------------


def test_stale_window_shows_the_percentage_with_its_age():
    # Arrange
    pct = 3.0
    # Act
    line = render_window_line(
        "7d", pct, hint="", hint_width=0, state=STALE, age_seconds=86_400
    )
    # Assert
    assert "(3% stale 1d)" in line


def test_known_window_shows_a_bare_percentage():
    # Arrange
    pct = 3.0
    # Act
    line = render_window_line("7d", pct, hint="", hint_width=0, state=KNOWN)
    # Assert
    assert "(3%)" in line


# ---------------------------------------------------------------------------
# the block explains itself
# ---------------------------------------------------------------------------


def test_block_states_why_a_reading_is_not_known():
    # Arrange — "network down" and "wrong account" need opposite responses,
    # and a bare `unknown` cannot tell them apart.
    row = _row(
        "ywatanabe-scitex-ai",
        None,
        UNKNOWN,
        usage_reason="credential belongs to ywata1989@gmail.com",
    )
    # Act
    block = render_account_block(row, hint_5h="", hint_7d="", hint_width=0)
    # Assert
    assert block[-1].strip() == "! credential belongs to ywata1989@gmail.com"


def test_known_block_has_no_explanation_line():
    # Arrange
    row = _row("acct", 3.0, KNOWN)
    # Act
    block = render_account_block(row, hint_5h="", hint_7d="", hint_width=0)
    # Assert
    assert len(block) == 3


# ---------------------------------------------------------------------------
# the fleet average refuses to count what it cannot vouch for
# ---------------------------------------------------------------------------


def test_average_counts_only_known_rows():
    # Arrange — the incident: one real account, one duplicate of it.
    rows = [_row("real", 3.0, KNOWN), _row("twin", None, UNKNOWN)]
    # Act
    block = render_average_block(rows, hint_width=0)
    # Assert
    assert block[0].startswith("- Average (n=1)")


def test_average_states_how_many_rows_it_declined_to_count():
    # Arrange
    rows = [_row("real", 3.0, KNOWN), _row("twin", None, UNKNOWN)]
    # Act
    block = render_average_block(rows, hint_width=0)
    # Assert
    assert "1 of 2 not counted" in block[0]


def test_average_excludes_stale_rows_from_the_mean():
    # Arrange — 3 % known and 99 % stale must not average to 51 %.
    rows = [_row("fresh", 3.0, KNOWN), _row("old", 99.0, STALE)]
    # Act
    block = render_average_block(rows, hint_width=0)
    # Assert
    assert "(3%)" in block[1]


def test_average_is_unknown_when_no_row_can_be_counted():
    # Arrange — must NOT render an empty bar, which reads as 0 %.
    rows = [_row("a", None, UNKNOWN), _row("b", None, UNKNOWN)]
    # Act
    block = render_average_block(rows, hint_width=0)
    # Assert
    assert block == ["- Average (unknown — 2 of 2 not counted)"]
