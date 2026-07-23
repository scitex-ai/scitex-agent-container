"""Liveness-verdict announcement, extracted from ``_start.py`` (512-line cap)."""

from __future__ import annotations

__all__ = ["_announce_start_verdict"]


def _announce_start_verdict(verdict) -> None:
    """Print the liveness verdict + its evidence before a non-no-op start.

    An operator staring at ``running | pid=None`` learns nothing. This prints
    WHY — ``ALIVE (delivery: 1 live inbox subscriber)`` / ``UNKNOWN (heartbeat:
    beat is 5086s stale …)`` — and, on UNKNOWN, says plainly that we are
    starting the agent anyway and that doing so destroys nothing.
    """
    import sys as _sys

    print(f"[sac:liveness] {verdict.agent}: {verdict.render()}", file=_sys.stderr)
    if verdict.is_unknown:
        print(
            f"[sac:liveness] cannot CONFIRM '{verdict.agent}' is alive, and "
            f"nothing answers for it — starting it. This is NOT destructive: "
            f"if it is in fact alive, the runtime's own duplicate-session "
            f"guard no-ops instead of relaunching over it. (`--force` is the "
            f"only verb that tears an existing session down.)",
            file=_sys.stderr,
        )
