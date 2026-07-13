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

# Ceiling for the ONE batched fleet probe. A healthy ``tmux
# list-sessions`` returns in single-digit ms even with ~100 sessions; this
# only ever fires on a genuinely wedged tmux server. Bounding the
# subprocess itself (not just the caller's off-loop guard) matters: an
# UNBOUNDED child would keep running inside an abandoned executor thread,
# leaking a worker slot out of the SHARED default thread-pool that
# ``agent_restart`` / ``host_exec`` also dispatch through.
BATCH_PROBE_TIMEOUT_S = 5.0

# The two ways tmux reports "there is no server, hence zero sessions" — a
# CONFIRMED-empty fleet, NOT an unknown (see :func:`list_sessions_activity`).
# Verified against tmux directly rather than assumed:
#   * socket file absent  -> "error connecting to /tmp/tmux-1000/x
#                             (No such file or directory)"   [rc=1, stderr]
#   * socket present, no server listening
#                         -> "no server running on ..."      [rc=1, stderr]
# ANY OTHER failure stays UNKNOWN (``None``): mis-reading a wedged probe as
# an empty fleet is exactly what marks live agents dead.
_NO_SERVER_MARKERS = (
    "no server running",
    "no such file or directory",
)


def _is_no_server(stderr: str) -> bool:
    """True iff ``stderr`` is tmux's definitive "no server ⇒ no sessions"."""
    low = (stderr or "").lower()
    return any(marker in low for marker in _NO_SERVER_MARKERS)


def list_sessions_activity(
    *, timeout_s: float = BATCH_PROBE_TIMEOUT_S, socket_name: str | None = None
) -> dict[str, int] | None:
    """Return ``{session_name: activity_epoch}`` for EVERY session, in ONE probe.

    The batched replacement for the per-agent ``exists`` + ``session_activity``
    pair. Those cost THREE ``tmux`` subprocess spawns per agent (``exists``
    spawns one; ``session_activity`` goes through :func:`_display_field`,
    which re-probes ``exists`` and then spawns ``tmux display``), so a
    heartbeat tick was O(N) subprocess spawns — ~30s at fleet scale on a
    loaded host, which blew the tick's budget and got it ABANDONED, leaving
    the whole fleet's liveness data stale.

    This is O(1): one ``tmux list-sessions -F`` returns every session name
    with its activity epoch. Same shape as
    :func:`_state.port_allocator.list_claims` (one query for all rows,
    callers look up per row).

    RETURN CONTRACT — "unknown" and "empty" are DIFFERENT, and the
    difference is load-bearing:

      * ``dict``  — the probe SUCCEEDED. Keys are the live sessions. A
        session absent from the dict is CONFIRMED absent.
      * ``{}``    — the probe succeeded and the fleet is genuinely empty
        (no tmux server ⇒ no sac TUI session can exist).
      * ``None``  — the probe FAILED (timeout / wedged tmux / unreadable
        output). Liveness is UNKNOWN. Callers MUST NOT read this as "every
        agent is dead" — they must leave the previous liveness data alone.

    ``socket_name`` targets a non-default tmux socket (``-L``); production
    leaves it ``None``. It exists so tests can drive THIS function against
    an isolated server instead of re-implementing the parse (a test copy
    silently drifts from the code it claims to cover).

    Never raises.
    """
    argv = ["tmux"]
    if socket_name:
        argv += ["-L", socket_name]
    argv += ["list-sessions", "-F", "#{session_name}\t#{session_activity}"]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s
        )
    except (OSError, subprocess.SubprocessError):  # stx-allow: fallback (a wedged/missing tmux is UNKNOWN liveness, never "all agents dead" — the caller preserves the previous data)
        return None
    if result.returncode != 0:
        # Distinguish "no tmux server" (a definitive, EMPTY fleet) from any
        # other failure (UNKNOWN). Reading a wedged probe as an empty fleet
        # is exactly the bug this contract prevents.
        if _is_no_server(result.stderr or ""):
            return {}
        return None
    out: dict[str, int] = {}
    for line in (result.stdout or "").splitlines():
        name, sep, raw = line.partition("\t")
        if not sep or not name:
            continue
        try:
            out[name] = int(raw.strip())
        except ValueError:  # stx-allow: fallback (one unparseable activity stamp contributes nothing — the other sessions still beat)
            continue
    return out


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
