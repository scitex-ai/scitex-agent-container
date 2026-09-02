"""Rendering for ``sac doctor --timers``.

Lives beside :mod:`.doctor_cmds` rather than inside it for the reason every
other split in this package happened: that module is at its line cap, and a
check's PALETTE and its ROW FORMAT are presentation, not diagnosis. The
verdict itself is computed in :mod:`.._maintenance._timer_liveness_probe`
and can be read with no terminal anywhere near it.
"""

from __future__ import annotations

from .._maintenance._timer_liveness_model import (
    ARMING_OK,
    ARMING_UNKNOWN,
    ARMING_VIOLATION,
    TIMER_ARMED,
    TIMER_NEVER_AGAIN,
    TIMER_UNKNOWN,
    TimerLivenessVerdict,
)
from .._maintenance._timer_liveness_model import SCOPE_NOTE as TIMER_SCOPE_NOTE
from ._helpers import console

__all__ = ["render_timers_human"]

# The same three-valued palette the neighbouring doctor checks use. UNKNOWN
# is yellow because `systemctl --user show` needs a user bus that a container
# does not have: a run with no vantage must not render like a host that came
# back clean.
_TIMER_STYLE = {
    ARMING_OK: "green",
    ARMING_VIOLATION: "red",
    ARMING_UNKNOWN: "yellow",
}

# Per-unit row colour. ARMED is dim rather than green: the rows exist to make
# the POPULATION visible, and twenty green lines would bury the one red one.
_TIMER_ROW_STYLE = {
    TIMER_ARMED: "dim",
    TIMER_NEVER_AGAIN: "red",
    TIMER_UNKNOWN: "yellow",
}


def render_timers_human(verdict: TimerLivenessVerdict) -> None:
    """Print the timer-liveness verdict, and its remedy when alarming.

    Every examined unit gets a row, not only the offenders. The fault this
    check exists for is INVISIBLE to every other instrument — the dead units
    read `enabled`, `active` and `Result=success` — so a reader needs to see
    that the check looked at their timer at all before a clean verdict means
    anything to them.
    """
    style = _TIMER_STYLE.get(verdict.state, "white")
    console.print(f"[bold]user timers[/bold]  [{style}]{verdict.summary()}[/{style}]")
    for reading in verdict.readings:
        row = _TIMER_ROW_STYLE.get(reading.state, "white")
        console.print(
            f"  [{row}]{reading.state:<12}[/{row}] {reading.unit}  "
            f"[dim]{reading.why}[/dim]"
        )
    console.print(f"  [dim]{verdict.population()}[/dim]")
    console.print(f"  [dim]{TIMER_SCOPE_NOTE}[/dim]")
    if verdict.is_alarming:
        console.print(f"  [bold]hint[/bold]  {verdict.hint()}")
