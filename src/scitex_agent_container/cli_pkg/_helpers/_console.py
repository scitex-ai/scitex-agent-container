"""Shared rich Console + sac-system status-line printer."""

from __future__ import annotations

from rich.console import Console

console = Console()


def system_msg(text: str, style: str = "blue") -> None:
    """Print a sac-system status line wrapped in ``=== ... ===``.

    Use for lifecycle announcements (start / stop / restart / delete
    progress, force-mode notice, etc.). Agent-voice output stays
    unwrapped so the frame marks the sac-system boundary clearly.
    """
    console.print(f"[{style}]=== [sac] {text} ===[/{style}]")
