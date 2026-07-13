"""Pure liveness / responsiveness decisions for the TUI runtime.

Split out of :mod:`runtimes.tui_session` (512-line per-file cap) so the
identity-based liveness rule has room for its rationale and its own unit
suite. All functions are pure (collaborators injected) so they test
without tmux.

ROOT CAUSE (card ``sac-fix-live-agents-read-stopped``, reproduced
2026-07-08): the old ``TuiSessionRuntime.is_running`` gated on
``session_activity`` freshness (pane I/O within a 300s max-idle window).
tmux advances ``#{session_activity}`` only on pane output OR input, so a
live-but-quiet agent sitting at its input prompt froze that stamp — every
such agent read ``stopped`` minutes after its last visible output
(empirically: ``session_activity`` 3.5h stale for a provably-live,
tmux-attached session with an alive ``apptainer exec ... claude`` pane
process). LIVENESS and RESPONSIVENESS were conflated.

FIX: separate the two.

  * :func:`pane_process_alive` — LIVENESS. The ``tui-<name>`` session
    exists AND its pane's process is alive (``os.kill(pane_pid, 0)`` —
    identity-based, namespace-robust: it sees a live pid where
    ``ps -p <pid>`` returns empty on this host). No activity gate — an
    idle agent is still running. This backs ``is_running`` (the status /
    ``sac agents list`` signal).
  * :func:`is_responsive_from_activity` — RESPONSIVENESS. The old
    activity-freshness rule, preserved for any hang-detection consumer
    that genuinely wants "moving", now behind ``is_responsive``.
"""

from __future__ import annotations

import os
from typing import Callable, Optional


def pid_alive(pid: int | None) -> bool:
    """True iff ``pid`` is a live process (``os.kill(pid, 0)``).

    ``None`` / non-positive pids are NOT a liveness verdict on their own —
    callers treat "unreadable pid" as "defer to session existence", so
    this returns ``False`` for them and the caller decides. A
    ``PermissionError`` means the pid exists but is owned by another user
    → alive.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def pane_process_alive(
    session_name: str,
    *,
    exists_fn: Callable[[str], bool],
    pane_dead_fn: Optional[Callable[[str], bool | None]] = None,
    pane_pid_fn: Optional[Callable[[str], int | None]] = None,
) -> bool:
    """LIVENESS: the session exists AND its pane process is alive.

    Decision order:
      1. session absent → ``False``.
      2. ``pane_dead_fn`` reports ``True`` (retained-dead pane,
         ``remain-on-exit``) → ``False``.
      3. ``pane_pid_fn`` yields a concrete pid → ``os.kill(pid, 0)``
         verdict (identity/namespace-robust).
      4. no pid probe available, or pid unreadable (``None``) → ``True``:
         session-exists is itself a liveness signal (sac never sets
         ``remain-on-exit``, so a dead pane normally closes its window and
         the session disappears). This also keeps back-compat with an
         injected multiplexer fake that predates the pane-pid probe.
    """
    if not exists_fn(session_name):
        return False
    if pane_dead_fn is not None:
        try:
            if pane_dead_fn(session_name) is True:
                return False
        except Exception:  # stx-allow: fallback (a pane_dead probe hiccup must not flip a live session to dead — defer to the pid check)
            pass
    if pane_pid_fn is None:
        return True
    try:
        pid = pane_pid_fn(session_name)
    except Exception:  # stx-allow: fallback (a pane_pid probe hiccup defers to session-exists = alive, never a false "stopped")
        return True
    if pid is None:
        return True
    return pid_alive(pid)


def pane_pid_of(
    session_name: str,
    *,
    pane_pid_fn: Optional[Callable[[str], int | None]] = None,
) -> int | None:
    """The LONG-LIVED pid backing a TUI session's pane, or ``None``.

    This is the value ``instances.pid`` records for a TUI agent, and it
    is the SAME signal :func:`pane_process_alive` (hence
    ``TuiSessionRuntime.is_running``) already keys liveness on — so the
    registry and ``is_running`` can never disagree about which process
    represents this agent.

    Why the PANE pid and not the launcher's: the pane's ``bash -c``
    ``exec``s apptainer, and ``exec`` REPLACES the process image while
    KEEPING the pid. So ``#{pane_pid}`` is stable from the moment the
    pane exists and *is* the ``apptainer exec ... claude`` process for
    the whole session (see :mod:`_runners._tmux._tmux_probe`). The
    launcher that created the tmux session, by contrast, returns
    immediately — recording it would store a pid that is dead within
    seconds.

    ``None`` (honest "unknown") when the session is absent, no pid probe
    is available (an injected multiplexer fake predating the probe), or
    the probe fails. Never raises, and never guesses a pid.
    """
    if pane_pid_fn is None:
        return None
    try:
        return pane_pid_fn(session_name)
    except Exception:  # stx-allow: fallback (a pane_pid probe hiccup yields "unknown" — a fabricated pid would be strictly worse than None, since a wrong/reused pid can vouch for a dead agent as alive)
        return None


def is_responsive_from_activity(
    activity: int | float | None,
    now: float,
    max_idle_s: float,
) -> bool:
    """RESPONSIVENESS: pane activity within ``max_idle_s`` of ``now``.

    ``None`` activity (no readable stamp / absent session) → ``False``.
    This is the OLD ``is_running`` rule, kept for hang-detection.
    """
    if activity is None:
        return False
    return (now - float(activity)) <= max_idle_s


__all__ = [
    "pid_alive",
    "pane_pid_of",
    "pane_process_alive",
    "is_responsive_from_activity",
]
