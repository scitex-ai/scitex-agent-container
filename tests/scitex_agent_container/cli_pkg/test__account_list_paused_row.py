"""A pause the operator cannot SEE is a trap, so the listing has to show it.

He said 「また復活させる」 — he intends to bring these accounts back.
A pause never expires and nothing nags him about one, so the screen he
already watches (``sac accounts list``, refreshed every few seconds) is
the only standing reminder that an account is resting rather than
broken.

THE TOKEN TTL IS REPLACED, NOT JOINED. The Status cell normally reads
``VALID +2h26m``, and that ``+2h26m`` belongs to the TOKEN. Printing it
beside PAUSED — ``PAUSED +6h12m`` — would be read as "the pause expires
in six hours", which is a lie about the one property that makes a pause
trustworthy: nothing lifts it but the operator.
:func:`test_a_paused_row_does_not_show_the_token_ttl` is that rule, and
:func:`test_an_unpaused_row_still_shows_the_token_ttl` is its control —
without the second, a bug that dropped the TTL from every row would
pass the first.

Pure renderer tests: an ``AccountRow`` is hand-rolled and pushed through
the real table renderer. No CliRunner, no store, no clock patched — the
dataclass exists precisely so this can be done with nothing
substituted, and both new fields default so every pre-existing
hand-rolled row in the suite still constructs.
"""

from __future__ import annotations

import time

import pytest

from scitex_agent_container.cli_pkg._account_list_format import format_ttl_live
from scitex_agent_container.cli_pkg._account_list_render import (
    _PAUSE_REASON_CHARS,
    AccountRow,
    _fmt_status,
    render_stored_table_to_str,
)

_HOURS_LEFT = 6.2


def _row(**overrides) -> AccountRow:
    base = dict(
        name="beta-example-com",
        freshness_state="VALID",
        freshness_hours=_HOURS_LEFT,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=None,
    )
    base.update(overrides)
    return AccountRow(**base)


@pytest.fixture
def paused_table() -> str:
    """One row for an account the operator rested three days ago."""
    row = _row(
        pause_reason="quota rest",
        pause_since=time.time() - 3 * 86400,
    )
    return render_stored_table_to_str([row], width=160)


@pytest.fixture
def unpaused_table() -> str:
    """The identical row with no pause — the control for every assertion."""
    return render_stored_table_to_str([_row()], width=160)


def test_a_paused_row_says_paused(paused_table: str):
    # Arrange
    table = paused_table
    # Act
    shown = "PAUSED" in table
    # Assert
    assert shown is True


def test_an_unpaused_row_does_not_say_paused(unpaused_table: str):
    """The control: PAUSED must come from the pause, not from the renderer."""
    # Arrange
    table = unpaused_table
    # Act
    shown = "PAUSED" in table
    # Assert
    assert shown is False


def test_a_paused_row_quotes_the_operators_reason(paused_table: str):
    """"PAUSED" alone would leave him wondering which decision this was."""
    # Arrange
    table = paused_table
    # Act
    shown = "quota rest" in table
    # Assert
    assert shown is True


def test_a_paused_row_shows_the_age_of_the_decision(paused_table: str):
    # Arrange
    table = paused_table
    # Act
    shown = "PAUSED 3d" in table
    # Assert
    assert shown is True


def test_a_paused_row_does_not_show_the_token_ttl(paused_table: str):
    """``PAUSED +6h12m`` would read as "the pause expires in 6h12m"."""
    # Arrange
    ttl = format_ttl_live(_HOURS_LEFT)
    # Act
    shown = ttl in paused_table
    # Assert
    assert shown is False


def test_an_unpaused_row_still_shows_the_token_ttl(unpaused_table: str):
    """The control for the assertion above."""
    # Arrange
    ttl = format_ttl_live(_HOURS_LEFT)
    # Act
    shown = ttl in unpaused_table
    # Assert
    assert shown is True


def test_a_long_reason_is_truncated_rather_than_wrapping_the_table():
    """The cell has neighbours; an essay in it pushes them off the screen.

    ASSERTED ON THE CELL, AND THIS TEST USED TO BE UNABLE TO FAIL.
    Reviewed 2026-08-26: it rendered a 200-character reason into a
    160-column table and asserted ``"x" * 200 not in table``. ``rich``
    wraps a long cell across lines, so that run of 200 x's can never
    appear contiguously in a rendered table no matter what this code
    does — deleting the truncation entirely left the file's seven tests
    green and the whole list-render suite green (69 passed), while the
    operator got a Status cell that shoved Identity and Last-update off
    his screen. The assertion was measuring rich's line breaking.

    :func:`_fmt_status` is what the application controls, so the bound
    goes there. ``+ 20`` is head-room for the fixed prefix (``PAUSED``,
    the age, the em-dash and the ellipsis), not slack in the claim: the
    number that matters is that the cell is bounded by
    ``_PAUSE_REASON_CHARS`` rather than by the reason's own length.
    """
    # Arrange
    reason = "x" * 200
    # Act
    cell = _fmt_status("VALID", _HOURS_LEFT, pause_reason=reason, pause_since=1.0)
    # Assert
    assert len(cell) <= _PAUSE_REASON_CHARS + 20


def test_a_short_reason_survives_intact(paused_table: str):
    """The reversing control: truncation must not eat a normal reason.

    Without it, a "fix" that returned a constant would satisfy the
    bound above and lose the only thing the cell is carrying.
    """
    # Arrange
    expected = "quota rest"
    # Act
    shown = expected in paused_table
    # Assert
    assert shown is True
