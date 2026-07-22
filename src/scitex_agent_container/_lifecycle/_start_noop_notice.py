"""Renderer for the "already running, nothing launched" start notice."""

from __future__ import annotations

__all__ = ["render_already_running"]


def render_already_running(name: str, evidence: str) -> str:
    """State + the commands that WOULD act, for the idempotent-start no-op.

    Every hinted command is verified to run as written: ``restart`` refuses
    without ``-y``, ``stop`` does not (its ``-y`` gate covers only the
    fleet-wide selection flags).
    """
    return "\n".join(
        (
            f"{name} is already running [{evidence}] — nothing launched",
            f"  - restart it:          sac agents restart {name} -y",
            f"  - force a fresh start: sac agents start {name} --force",
            f"  - stop, then start:    sac agents stop {name} "
            f"&& sac agents start {name}",
        )
    )
