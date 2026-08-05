"""Liveness-verdict announcement, extracted from ``_start.py`` (512-line cap)."""

from __future__ import annotations

__all__ = ["_announce_start_verdict"]


def _announce_start_verdict(verdict) -> None:
    """Announce the liveness verdict + its evidence before a non-no-op start.

    An operator staring at ``running | pid=None`` learns nothing, so this says
    WHY — ``ALIVE (delivery: 1 live inbox subscriber)`` / ``UNKNOWN (heartbeat:
    beat is 5086s stale …)`` — and, on UNKNOWN, states plainly that we start it
    anyway and that doing so destroys nothing.

    Goes through ``system_msg`` rather than ``print``: the level prefix is what
    tells a reader at a glance whether a line needs attention, and a bare print
    has none. The old ``[sac:liveness]`` literal is dropped with it — it was a
    hand-rolled prefix standing in for the level, and printing both would say
    the same thing twice.

    The verdict LEVEL is not always a problem: on the restart path a DEAD
    verdict is the expected reading (we just stopped the agent), so it is
    reported at ``info`` and only the genuinely ambiguous UNKNOWN escalates to
    ``warn``. Rendering an expected condition as an alarm is how an operator
    learns to ignore the line.
    """
    from ..cli_pkg._helpers._console import system_msg

    system_msg(f"{verdict.agent}: {verdict.render()}", style="info")
    if verdict.is_unknown:
        system_msg(
            f"cannot CONFIRM '{verdict.agent}' is alive, and nothing answers "
            f"for it — starting it. NOT destructive: if it is in fact alive, "
            f"the runtime's own duplicate-session guard no-ops instead of "
            f"relaunching over it (`--force` is the only verb that tears an "
            f"existing session down).",
            style="warn",
        )
