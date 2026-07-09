"""Read-only tmux pane probes — identity-based liveness signals.

Extracted from :mod:`_runners._tmux.tmux` (512-line per-file cap) so the
new ``pane_pid`` / ``pane_dead`` probes have room to live with their
rationale. ``TmuxManager.pane_pid`` / ``.pane_dead`` are thin delegates
over these pure functions; ``TuiSessionRuntime.is_running`` keys its
liveness decision off ``pane_pid`` (card
``sac-fix-live-agents-read-stopped``: an idle-but-alive TUI advanced its
``session_activity`` stamp only on pane I/O, so the old
activity-freshness gate read a live-but-quiet agent as ``stopped`` after
the max-idle window — a false negative for every agent sitting at its
input prompt).

The pane-pid signal is IDENTITY-based (a concrete OS process) and
NAMESPACE-robust: ``os.kill(pane_pid, 0)`` sees the live
``apptainer exec ... claude`` process across the PID-namespace /
``ps -p <pid>``-filter boundaries where a plain ``ps -p`` returns empty
for a still-live pid on this host (card lead #2).
"""

from __future__ import annotations

import subprocess


def _display_field(session_name: str, fmt: str) -> str | None:
    """Return ``tmux display -p '<fmt>'`` for ``session_name`` (stripped),
    or ``None`` when the session is absent / the probe fails.

    Local import of :class:`TmuxManager` keeps the existence check on the
    one canonical implementation without a module-level import cycle.
    """
    from .tmux import TmuxManager

    if not TmuxManager.exists(session_name):
        return None
    result = subprocess.run(  # pragma: no cover  -- requires live tmux, not available on CI runner
        ["tmux", "display", "-p", "-t", session_name, fmt],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover  -- requires live tmux
        return None
    raw = result.stdout.strip()  # pragma: no cover  -- requires live tmux
    return raw or None  # pragma: no cover  -- requires live tmux


def pane_pid(session_name: str) -> int | None:
    """Return the PID of the process in the session's active pane, or
    ``None`` when no such session exists / the probe fails.

    For a sac-owned TUI session this is the long-lived
    ``apptainer exec ... claude`` process: the pane's ``bash -c``
    ``exec``s apptainer, so after exec ``pane_pid`` IS the apptainer /
    claude PID. This is the identity signal
    ``TuiSessionRuntime.is_running`` checks for liveness. Never raises.
    """
    raw = _display_field(session_name, "#{pane_pid}")
    if raw is None:  # pragma: no cover  -- requires live tmux
        return None
    try:  # pragma: no cover  -- requires live tmux
        return int(raw)
    except ValueError:  # pragma: no cover  -- requires live tmux
        return None


def session_activity(session_name: str) -> int | None:
    """Return the unix-epoch stamp of the session's last pane activity,
    or ``None`` when absent. Backed by ``#{session_activity}`` (tmux
    advances it on pane output OR input).

    NOTE: this is a RESPONSIVENESS / movement signal, NOT a liveness
    signal — an idle-but-alive TUI does not advance it. It backs
    :meth:`TuiSessionRuntime.is_responsive`; liveness (``is_running``)
    keys off :func:`pane_pid` instead.
    """
    raw = _display_field(session_name, "#{session_activity}")
    if raw is None:  # pragma: no cover  -- requires live tmux
        return None
    try:  # pragma: no cover  -- requires live tmux
        return int(raw)
    except ValueError:  # pragma: no cover  -- requires live tmux
        return None


def pane_dead(session_name: str) -> bool | None:
    """Return whether the active pane's process EXITED while the pane is
    retained (``remain-on-exit``), or ``None`` when absent / probe fails.

    ``tmux`` reports ``#{pane_dead}`` == ``1`` for a retained-dead pane.
    sac does not set ``remain-on-exit`` (a dead pane normally closes its
    window and the session disappears), so this is a belt-and-suspenders
    guard letting ``is_running`` report ``stopped`` for a retained corpse
    rather than a false ``running``.
    """
    raw = _display_field(session_name, "#{pane_dead}")
    if raw is None:  # pragma: no cover  -- requires live tmux
        return None
    return raw == "1"  # pragma: no cover  -- requires live tmux


__all__ = ["pane_pid", "pane_dead", "session_activity"]
