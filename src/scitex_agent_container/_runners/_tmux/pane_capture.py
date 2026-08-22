"""Layer 1 — data: pane_capture(name) -> str.

Pure read; no side effects. Captures the current pane content for the
named agent's tmux session on server ``-L sac``, session ``sac-<name>``.
"""

from __future__ import annotations

import subprocess

from ._target import exact_target

_MAX_CHARS = 10_000
_TMUX_SERVER = "sac"


def pane_capture(name: str, max_chars: int = _MAX_CHARS) -> str:
    """Return the current pane content for agent *name*.

    Targets tmux server ``-L sac``, session ``sac-<name>``.
    Returns an empty string on any error (session missing, tmux absent, etc.).
    """
    session = f"sac-{name}"
    try:
        result = subprocess.run(
            [
                "tmux",
                "-L",
                _TMUX_SERVER,
                "capture-pane",
                "-t",
                exact_target(session),
                "-p",
                "-J",
            ],
            capture_output=True,
            text=True,
        )
        text = result.stdout or ""
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return ""
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text
