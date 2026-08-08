"""Pure, fully-injectable modal-drain primitives for the TUI runtime.

Extracted from :mod:`tui_session` (which re-exports the public names, so
existing imports keep working) to keep that module under the line limit and to
group the unit-testable modal-drain logic — no tmux import — in one cohesive
place, mirroring :mod:`_tui_compose`. :class:`TuiSessionRuntime` wires the real
tmux callables into these functions.

Three boot fixes live here (card
``sac-boot-automation-devchannels-modal-continue-compose-buffer``):

  * **BUG 1 (Esc-cancel / ordering)** — the drain dismisses the
    ``--dangerously-load-development-channels`` confirmation (and any modal) by
    its registered keys (Enter → option 1), NEVER by ``Escape``. It VERIFIES a
    modal cleared (re-capture + :func:`prompts.detect`) before moving on. The
    dangerous ``Escape``-based compose-buffer clear lives in :mod:`_tui_compose`
    and only runs once :func:`prompts.has_esc_cancel_modal` is False — so
    dev-channels is always resolved before any Esc.
  * **BUG 2 (Ink drops input while re-rendering a large continue session)** —
    before sending a modal's keys, WAIT FOR THE PANE TO SETTLE (no change in
    captured content for a short quiet-period). A large ``--continue`` session
    replays its history and the Ink TUI drops keystrokes mid-render; sending
    into a settled pane is what makes the Enter land. Per-modal patience is
    bounded and fail-loud.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import prompts as _prompts
from ._pane_context_log import log_pane_fault

__all__ = [
    "drain_modals_until_ready",
    "wait_for_settle",
    "wait_until_input_ready",
]

_MARKER = "? for shortcuts"


def _resolve_settle(
    poll_s: float,
    settle_quiet_s: float | None,
    settle_max_s: float | None,
) -> tuple[float, float]:
    """Derive the BUG-2 settle windows from ``poll_s`` when not given explicitly.

    Tying settle to the poll cadence keeps ONE knob: production polls at
    ``poll_s=0.5`` → a 2s quiet window / 8s cap (enough to ride out a large
    ``--continue`` re-render), while a fast unit drain at ``poll_s=0`` gets a
    ZERO settle (a single capture, no wait) so short test timeouts are not eaten
    by the settle spin. Explicit values always win.
    """
    quiet = settle_quiet_s if settle_quiet_s is not None else poll_s * 4
    cap = settle_max_s if settle_max_s is not None else poll_s * 16
    return quiet, cap


def wait_for_settle(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    quiet_s: float,
    max_wait_s: float,
    poll_s: float = 0.4,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Block until the pane content stops changing for ``quiet_s``, then
    return the last captured pane. Bounded by ``max_wait_s``.

    BUG 2 fix: a large ``--continue`` session replays its transcript and the
    Ink TUI keeps re-rendering — new text keeps arriving — and it DROPS
    keystrokes sent during that window. Sending a modal's keys into a settled
    (quiet) pane is what makes them land. This waits for a stable window
    (``quiet_s`` with no captured-content change) before the caller sends keys,
    bounded by ``max_wait_s`` so a genuinely-busy pane (spinner animating
    forever) does not block boot indefinitely — the caller sends after the cap
    and the verify/resend loop is the second net.

    Returns the most recent capture regardless of whether it settled (the
    caller acts on it either way).
    """
    deadline = time_fn() + max_wait_s
    last = capture_fn(name)
    stable_since = time_fn()
    while time_fn() < deadline:
        if poll_s > 0:
            sleep_fn(poll_s)
        current = capture_fn(name)
        if current == last:
            if (time_fn() - stable_since) >= quiet_s:
                return current
        else:
            last = current
            stable_since = time_fn()
    return last


def drain_modals_until_ready(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    send_keys_fn: Callable[[str], None],
    exists_fn: Callable[[str], bool],
    timeout_s: float,
    poll_s: float = 0.5,
    settle_quiet_s: float | None = None,
    settle_max_s: float | None = None,
    max_resends: int = 6,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Verified, retrying, fail-loud modal drain. True iff ready in window.

    Pure + fully injectable — no tmux import. Wired to the real tmux callables
    by :meth:`TuiSessionRuntime._drain_modals_until_ready`.

    Each loop:

      1. capture the pane; exit on the ready marker / :func:`prompts.is_ready`;
      2. fail FAST if the session died (``exists_fn`` False) — surface the last
         pane and abort (a dead session can never reach ready);
      3. :func:`prompts.detect` the on-screen modal (detect-only);
      4. **BUG 2** — before responding, :func:`wait_for_settle` so the Ink TUI
         is not mid-render (a large ``--continue`` replay drops keys otherwise);
         re-detect after settling in case the modal changed/cleared;
      5. :func:`prompts.respond_modal` (sends its registered keys — Enter/digit,
         NEVER Escape), then settle again and re-detect next loop — if the SAME
         modal is still up the keys were dropped, RESEND (up to ``max_resends``).

    A modal that survives ``max_resends`` verified resends, or a window timeout
    with a modal still up / no ready marker, is logged LOUD (error) with the
    modal name + pane tail + an actionable ``tmux attach`` hint — never a silent
    best-effort return.
    """
    settle_quiet_s, settle_max_s = _resolve_settle(poll_s, settle_quiet_s, settle_max_s)
    log = logging.getLogger(__name__)
    deadline = time_fn() + timeout_s
    resends: dict[str, int] = {}
    # Keep the most recent NON-empty pane: when the inner process EXITS the
    # session is torn down and ``capture_fn`` returns empty, which would
    # otherwise erase the actual exit error from every log below.
    last_pane = ""
    while time_fn() < deadline:
        # Fail FAST on session death — a dead session can never reach ready;
        # polling out the whole window here is a silent stall.
        if not exists_fn(name):
            log_pane_fault(
                log,
                name,
                last_pane,
                "TuiSessionRuntime: boot-drain ABORTED for %s — the inner "
                "claude process EXITED during boot (tmux session gone), so it "
                "can never reach ready. This is NOT a login wall or a timeout; "
                "read the last pane (logged next, at info) for the real cause. "
                "Reproduce live: `tmux attach -t %s`.",
                name,
                name,
            )
            return False
        pane = capture_fn(name)
        if pane.strip():
            last_pane = pane
        if _MARKER in pane or _prompts.is_ready(pane):
            return True
        modal = _prompts.detect(pane)
        if modal is None:
            # No known modal + not ready: claude is still rendering or running
            # startup_commands. Keep polling (no fallback action).
            if poll_s > 0:
                sleep_fn(poll_s)
            continue
        n = resends.get(modal, 0)
        if n >= max_resends:
            log_pane_fault(
                log,
                name,
                last_pane or pane,
                "TuiSessionRuntime: boot-drain STUCK on modal %r for %s — its "
                "keystrokes did NOT dismiss it after %d verified resends. The "
                "detector/keys in runtimes/prompts.py are likely stale for this "
                "claude build, or the Ink TUI is dropping input. Inspect with "
                "`tmux attach -t %s`.",
                modal,
                name,
                n,
                name,
            )
            return False
        # BUG 2 — wait for the pane to SETTLE before sending keys, so a large
        # --continue replay's re-render race does not eat them. Re-detect after
        # settling: the modal may have changed or cleared while we waited.
        settled = wait_for_settle(
            name,
            capture_fn=capture_fn,
            quiet_s=settle_quiet_s,
            max_wait_s=settle_max_s,
            poll_s=poll_s,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
        )
        if settled.strip():
            last_pane = settled
        if _MARKER in settled or _prompts.is_ready(settled):
            return True
        modal_after = _prompts.detect(settled)
        if modal_after is None:
            # Cleared while settling — loop to re-evaluate readiness.
            continue
        _prompts.respond_modal(modal_after, send_keys_fn)
        resends[modal_after] = resends.get(modal_after, 0) + 1
        # Settle after the send so the next loop's re-detect sees the result of
        # the keystroke, not a mid-dismiss frame.
        if settle_quiet_s > 0:
            sleep_fn(min(settle_quiet_s, poll_s if poll_s > 0 else settle_quiet_s))
    # Window elapsed. Report what is ACTUALLY observable.
    pane = capture_fn(name)
    if pane.strip():
        last_pane = pane
    alive = exists_fn(name)
    stuck = _prompts.detect(last_pane)
    if stuck:
        diagnosis = f"Still showing modal {stuck!r} after resends (see errors above)."
    elif alive:
        diagnosis = (
            "Session is ALIVE but never signalled ready — claude is still "
            "mid-render, or is sitting at an UNHANDLED prompt (add a handler in "
            "runtimes/prompts.py for whatever the pane shows)."
        )
    else:
        diagnosis = (
            "Session has EXITED — the inner command died (read the last pane "
            "for the cause; this is NOT a credential/login problem)."
        )
    log_pane_fault(
        log,
        name,
        last_pane,
        "TuiSessionRuntime: boot-drain window (%.0fs) elapsed for %s without a "
        "ready signal. %s Reproduce live: `tmux attach -t %s`.",
        timeout_s,
        name,
        diagnosis,
        name,
    )
    return False


def wait_until_input_ready(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    send_keys_fn: Callable[[str], None],
    exists_fn: Callable[[str], bool],
    timeout_s: float = 60.0,
    poll_s: float = 0.4,
    settle_quiet_s: float | None = None,
    settle_max_s: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Drain first-launch / mid-session modals, then return True when the TUI
    input field is bound. Raises :class:`TuiInputNotReadyError` on timeout.

    Pure + fully injectable — no tmux import. Each polling frame is matched
    against the :mod:`runtimes.prompts` registry; the first matching handler's
    keys are sent (via ``send_keys_fn``) — Enter/digit, NEVER Escape, so a
    dev-channels ("Esc to cancel") modal is dismissed by CONFIRM, not cancel
    (BUG 1). Before sending we SETTLE the pane (BUG 2) so a large ``--continue``
    replay's re-render does not drop the keys. Settle windows default from
    ``poll_s`` (see :func:`_resolve_settle`).
    """
    from .._runners._tmux.tmux import TuiInputNotReadyError

    settle_quiet_s, settle_max_s = _resolve_settle(poll_s, settle_quiet_s, settle_max_s)
    if not exists_fn(name):
        raise TuiInputNotReadyError(
            f"TUI session {name!r} does not exist; nothing to wait for."
        )
    accepted: set[str] = set()
    deadline = time_fn() + timeout_s
    last_pane = ""
    while time_fn() < deadline:
        last_pane = capture_fn(name)
        if _MARKER in last_pane or _prompts.is_ready(last_pane):
            return True
        modal = _prompts.detect(last_pane)
        if modal is not None and modal not in accepted:
            # BUG 2 — settle before sending so the keys are not dropped
            # mid-render; re-detect after settling.
            settled = wait_for_settle(
                name,
                capture_fn=capture_fn,
                quiet_s=settle_quiet_s,
                max_wait_s=settle_max_s,
                poll_s=poll_s,
                sleep_fn=sleep_fn,
                time_fn=time_fn,
            )
            last_pane = settled
            if _MARKER in settled or _prompts.is_ready(settled):
                return True
            modal_after = _prompts.detect(settled)
            if modal_after is not None and modal_after not in accepted:
                _prompts.respond_modal(modal_after, send_keys_fn)
                accepted.add(modal_after)
                # Re-capture immediately on the next loop — the input field may
                # bind on the very next frame.
                continue
        if poll_s > 0:
            sleep_fn(poll_s)
    raise TuiInputNotReadyError(
        f"TUI input-ready marker {_MARKER!r} not seen in pane {name!r} within "
        f"{timeout_s:.1f}s after draining {len(accepted)} modal(s) "
        f"({sorted(accepted)}). Last pane content:\n{last_pane}"
    )
