"""Parser for ``spec.autonomous`` (F-CS3 phase 1)."""

from __future__ import annotations


def parse_autonomous(spec: dict):
    """Parse spec.autonomous (F-CS3 phase 1).

    Drive-until-done is opt-in: ``enabled`` defaults to False so
    every existing yaml continues to behave as a single-turn runner.
    Phase 2 will read these fields in _runners.claude_session.
    """
    from .._types import AutonomousSpec

    raw = spec.get("autonomous", {}) or {}
    if not isinstance(raw, dict):
        return AutonomousSpec()
    return AutonomousSpec(
        enabled=bool(raw.get("enabled", False)),
        drive_until=str(raw.get("drive_until", "DONE")),
        max_turns=int(raw.get("max_turns", 50)),
        idle_kick_after_s=int(raw.get("idle_kick_after_s", 120)),
        kick_text=str(raw.get("kick_text", "Continue. Print DONE when finished.")),
    )
