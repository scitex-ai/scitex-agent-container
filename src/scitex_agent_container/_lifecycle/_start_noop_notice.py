"""Renderer for the "already running, nothing launched" start notice."""

from __future__ import annotations

from typing import Any

__all__ = ["render_already_running", "render_start_noop_notice"]


def render_start_noop_notice(config: Any, verdict: Any) -> str:
    """The full no-op notice for ``agent_start``'s already-running branch.

    Resolves the tmux session sac owns for this agent (the SAME name the
    liveness verdict probed) and its pane pid, then delegates to
    :func:`render_already_running` so the notice NAMES what was found
    rather than only that something was. Both lookups are best-effort —
    a notice must never crash or block the no-op path it decorates.
    """
    from ._verdict_tmux import session_name_for_config

    session = session_name_for_config(config)
    # stx-allow: fallback (reason: the pane pid is best-effort colour on a
    # NOTICE — an unreadable pid folds away rather than blocking the no-op
    # path that is only telling the operator why nothing launched)
    try:
        from .._runners._tmux.tmux import TmuxManager

        pane_pid = TmuxManager.pane_pid(session)
    except Exception:  # stx-allow: fallback (reason: see above)
        pane_pid = None
    return render_already_running(
        getattr(config, "name", ""),
        verdict.render(),
        session=session,
        pane_pid=pane_pid,
    )


def render_already_running(
    name: str,
    evidence: str,
    *,
    session: str | None = None,
    pane_pid: int | None = None,
) -> str:
    """State + the commands that WOULD act, for the idempotent-start no-op.

    The first line NAMES WHAT WAS FOUND — agent, tmux session, pane pid —
    not just that something was. Incident 2026-08-14 (card
    ``sac-tmux-prefix-match-false-alive-20260814``): tmux prefix matching
    let a SIBLING session vouch for a dead agent, and a no-op that does not
    say WHICH session it believed in cannot be caught lying. With the
    session named, the operator reading "already running (tmux session
    tui-scitex-cards-gui...)" for agent scitex-cards sees the mismatch at a
    glance. ``session`` / ``pane_pid`` are best-effort: ``None`` folds the
    clause away rather than printing a fabricated placeholder.

    Every hinted command is verified to run as written: ``restart`` refuses
    without ``-y``, ``stop`` does not (its ``-y`` gate covers only the
    fleet-wide selection flags).
    """
    found = ""
    if session:
        pid_clause = f", pane pid {pane_pid}" if pane_pid else ""
        found = f" (tmux session {session}{pid_clause})"
    return "\n".join(
        (
            f"{name} is already running{found} [{evidence}] — nothing launched",
            f"  - restart it:          sac agents restart {name} -y",
            f"  - force a fresh start: sac agents start {name} --force",
            f"  - stop, then start:    sac agents stop {name} "
            f"&& sac agents start {name}",
        )
    )
