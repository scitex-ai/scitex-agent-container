"""The SCREEN signal — does the agent's tui pane show a FROZEN auth banner?

Split out of :mod:`._verdict_resolve` (512-line cap) along the SAME seam
:mod:`._verdict_state` follows: this reads a state ARTEFACT somebody else wrote
— here the ``sac agents auth-status`` cache — and turns it into a
:class:`._verdict.Signal`. :mod:`._verdict_resolve` PROBES live things (the
broker, the runtime); this READS a cache. That difference is why it is so
careful, and why every non-fresh / superseded / clean read degrades to
:data:`._verdict.UNKNOWN`, never a false WEDGE.

THE INSTRUMENT THIS EMBODIES
    Every other liveness sensor answers "IS IT PRESENT?" — a pid, a session, a
    row. None of them answers "IS IT WORKING?". A tmux-GREEN agent whose Claude
    sits under an auth-rejection banner has a live session and a live pane pid,
    so ``process`` / ``heartbeat`` / ``registry`` all read ALIVE while it does
    nothing (``scitex-clew`` sat that way for two days). This is the one sensor
    that reads the pane's rendered CONTENT — :data:`._verdict.INSTRUMENT_TUI_SCREEN`
    — so a wedged agent resolves to :data:`._verdict.WEDGED` instead of that
    false-green.

WHY IT READS A CACHE, NOT THE LIVE PANE
    The prompt-anchored, volatile-stripped, 2-run-frozen banner matcher lives in
    :mod:`.._runners._tmux.auth_status` and is run by the ``sac agents
    auth-status`` watchdog, which records its verdict via
    :func:`.._state.auth_state.record_auth_checks`. This module reads THAT
    through :func:`.._state.auth_state.verdict_for`, so all the freshness / scope
    honesty — SUPERSEDED (a verdict predating this incarnation is discarded) and
    STALE (older than 900s is weak, never asserted) — lives in one tested place.
    A WEDGED that reaches :func:`._verdict.decide` is therefore already fresh AND
    this-incarnation, and decide trusts it at face value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ._verdict import (
    INSTRUMENT_TUI_SCREEN,
    SOURCE_SCREEN,
    UNKNOWN,
    WEDGED,
    Signal,
)

__all__ = [
    "screen_signal",
    "started_at_for",
]


def started_at_for(name: str) -> str | None:
    """This incarnation's ``started_at`` stamp, for the screen SUPERSEDED check.

    Read from the SAME ``instances`` table :func:`._verdict_state.registry_signal`
    reads, so the stamp is directly comparable to the auth cache's ``checked_at``
    (both are ``state_db.now_iso`` UTC-'Z' stamps). ``list_active_instances``
    orders started_at DESC, so the newest row is this incarnation.

    Best-effort: any failure returns ``None`` — no started_at means no SUPERSEDED
    suppression, which is safe, because :func:`.._state.auth_state.verdict_for`
    still gates on staleness and on a missing row.
    """
    try:
        from .._state.state_db import list_active_instances

        rows = [r for r in list_active_instances(host=None) if r.get("name") == name]
    except Exception:  # stx-allow: fallback (an unreadable registry ⇒ no started_at ⇒ no SUPERSEDED suppression, never a crash)
        return None
    if not rows:
        return None
    return str(rows[0].get("started_at") or "") or None


def screen_signal(
    name: str,
    *,
    started_at: str | None = None,
    read_state: Callable[[str], dict | None] | None = None,
    now: datetime | None = None,
    stale_after_s: float | None = None,
) -> Signal:
    """Does the agent's rendered ``tui-<name>`` pane show a FROZEN auth banner?

    Reads the CACHED verdict, NOT the live pane (see the module docstring): the
    ``sac agents auth-status`` watchdog runs the banner matcher and records it,
    and this reads that cache through :func:`.._state.auth_state.verdict_for`.
    ALL freshness / scope gating lives in ``verdict_for`` (SUPERSEDED — a verdict
    stamped before ``started_at`` describes a previous incarnation and is
    discarded — and STALE > 900s), so a WEDGED that reaches
    :func:`._verdict.decide` is already fresh AND this-incarnation.

    Verdicts (:data:`._verdict.INSTRUMENT_TUI_SCREEN`, declared WEDGED/UNKNOWN
    only — never ALIVE, because a clean pane is not proof of life; never DEAD,
    because the pane and the process behind it are PRESENT):

    * no ``auth_checked_at`` (never swept, OR a SUPERSEDED row ``verdict_for``
      discarded) → :data:`._verdict.UNKNOWN`. The screen was not read; not
      evidence of life.
    * ``auth_check_stale`` (the watchdog has not swept recently) →
      :data:`._verdict.UNKNOWN`. A stale cache is never a stale WEDGE — an old
      banner may already be cleared.
    * ``auth_failed`` (a fresh frozen banner) → :data:`._verdict.WEDGED`, message
      carrying the banner + the remedy (a restart clears a rotated/stale token).
    * a fresh CLEAN pane → :data:`._verdict.UNKNOWN`. A clean pane is not proof
      of life, only the absence of a KNOWN wedge (the agent may be busy, idle, or
      stuck in a way this banner match does not recognise).

    ``read_state`` is the injection seam (default
    :func:`.._state.auth_state.get_auth_state`); tests pass a real callable
    returning a real cached-row dict. NO live tmux, NO clock of its own.
    """
    from .._state import auth_state as _auth_state

    reader = read_state or _auth_state.get_auth_state
    fields = _auth_state.verdict_for(
        reader(name),
        started_at=started_at,
        now=now,
        stale_after_s=stale_after_s or _auth_state.STALE_AFTER_S,
    )

    if not fields.get("auth_checked_at"):
        return Signal(
            SOURCE_SCREEN,
            UNKNOWN,
            f"no fresh auth-status check for {name!r} (never swept, or the cached "
            f"check predates this incarnation) — the screen was not read, which "
            f"is not evidence of life",
            INSTRUMENT_TUI_SCREEN,
        )
    if fields.get("auth_check_stale"):
        return Signal(
            SOURCE_SCREEN,
            UNKNOWN,
            "the cached auth-status check is STALE (the watchdog has not swept "
            "recently) — a stale cache is never a stale WEDGE, because the banner "
            "may already be cleared; report UNKNOWN, never a stale wedge",
            INSTRUMENT_TUI_SCREEN,
        )
    if fields.get("auth_failed"):
        banner = fields.get("auth_banner") or "an auth-rejection banner"
        remedy = fields.get("auth_remedy") or "restart"
        return Signal(
            SOURCE_SCREEN,
            WEDGED,
            f"a frozen auth banner sits above the prompt ({banner!r}) — the "
            f"tui-{name} pane is PRESENT but NOT WORKING; remedy: {remedy}",
            INSTRUMENT_TUI_SCREEN,
        )
    return Signal(
        SOURCE_SCREEN,
        UNKNOWN,
        "the auth-status check is fresh and CLEAN (no frozen banner) — but a "
        "clean pane is not proof of life, only the absence of a known wedge",
        INSTRUMENT_TUI_SCREEN,
    )
