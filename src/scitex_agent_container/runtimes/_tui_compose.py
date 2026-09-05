"""Pure, fully-injectable compose-buffer primitives for the TUI runtime.

Extracted from :mod:`tui_session` (which re-exports every public name here, so
existing imports keep working) to keep that module under the line limit and to
group the unit-testable Ink-TUI compose-buffer logic — no tmux import — in one
cohesive place. The :class:`TuiSessionRuntime` wires the real tmux callables
into these functions (``_verify_submitted`` → :func:`verify_submit_by_advancement`,
``_clear_compose_buffer`` → :func:`clear_compose_buffer`).

Boot fixes live here:

  * :func:`verify_submit_by_advancement` — the boot Enter-drop fix
    (sac-tui-enter-drop-on-boot): wait-for-idle + verify-by-advancement so the
    submit Enter never fires into the busy/initializing window where the Ink
    TUI silently drops it.
  * :func:`clear_compose_buffer` — the /compact-burst-on-restart fix
    (sac-tui-clear-compose-buffer-on-boot): a persistent tmux pane carries
    stale "compose-pending-unsent" text (external input that accumulated while
    the pane was busy) ACROSS an agent restart; the boot Enter would otherwise
    submit that whole stale stack. Clearing the compose buffer at the TOP of
    startup-prompt injection drops the stale text before the boot submit.
  * :func:`is_fresh_boot_welcome_screen` (+ its
    :func:`_wait_for_welcome_screen_to_clear` helper) — a second fix under the
    SAME card: on a FRESH (no ``--continue``) boot the first-launch
    welcome/model-info/promo screen can still be up, with the Ink TUI's input
    not yet bound, when :func:`clear_compose_buffer` runs — Escape sent into
    that window is not reliably delivered, so the clear would exhaust its
    resend budget against a screen it can never actually clear. Waiting for
    that screen to clear FIRST fixes the false "did NOT clear" failure without
    touching the already-working resumed-session path.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from ._pane_context_log import log_pane_fault
from ._pane_context_log import pane_tail as _pane_tail

__all__ = [
    "clear_compose_buffer",
    "is_fresh_boot_welcome_screen",
    "composer_holds_fragment",
    "fragment_tail",
    "verify_submit_by_advancement",
]


#: Live compose box = the BOTTOM-MOST ``❯`` row (claude renders it just
#: above the status bar). Same unsent-text regex as the shared
#: ``prompts`` detector, but scoped to that one row.
_COMPOSE_PROMPT_RE = re.compile(r"❯[ \t\xa0]+\S")

#: Keystrokes that EMPTY a (possibly multi-line) pending compose buffer in
#: Claude Code's Ink TUI WITHOUT submitting it or quitting the process.
#: EMPIRICALLY verified against a real ``claude`` 2.1.150 TUI
#: (sac-tui-clear-compose-buffer-on-boot, 2026-06-26): a single ``Escape``
#: leaves a multi-line buffer intact, but a DOUBLE ``Escape`` clears the whole
#: buffer in one shot — no per-line counting, no kill-ring side effect, and it
#: never fires Enter. (``C-u`` also clears, but only ONE line per press — it is
#: a line-kill, so an N-line stale stack needs N+ presses and leaves a
#: "Ctrl+Y to paste deleted text" hint; Esc-Esc is the clean single gesture.)
_COMPOSE_CLEAR_KEYS: tuple[str, ...] = ("Escape", "Escape")


def _compose_pending_live(pane: str) -> bool:
    """True iff the LIVE compose box holds pasted-but-unsent text.

    Scopes the ``compose-pending-unsent`` test to the CURRENT input line
    (the bottom-most ``❯`` row) instead of the whole pane. A ``❯`` sitting
    in SCROLLBACK — e.g. a submitted ``❯ 1`` echo or the prior turn's
    rendered prompt — otherwise makes the shared :func:`prompts.detect`
    report "still pending" forever, so :func:`verify_submit_by_advancement`
    never sees the buffer clear and false-alarms (the
    ``sac-tui-enter-drop-on-boot`` live test, 2026-06-24: scitex-dev booted
    fine yet the drive logged "stayed UNSENT after 8 attempts"). Text after
    the live ``❯`` = unsent; an empty live box = submitted / ready.
    """
    for row in reversed((pane or "").splitlines()):
        if "❯" in row:
            return bool(_COMPOSE_PROMPT_RE.search(row))
    return False


#: Compose-box markers, by harness. Claude Code's Ink TUI renders "❯";
#: Codex's composer renders "›" (U+203A). The live box is the BOTTOM-MOST
#: row carrying either.
_CLAUDE_COMPOSE_MARKER = "❯"
_CODEX_COMPOSE_MARKER = "›"
_COMPOSE_MARKERS = (_CLAUDE_COMPOSE_MARKER, _CODEX_COMPOSE_MARKER)

_WS_RUN_RE = re.compile(r"[\s\xa0]+")

#: How much of the pasted text to look for. The composer wraps and can
#: scroll its TOP away, so the TAIL is the part reliably on screen.
FRAGMENT_TAIL_CHARS = 60


def _normalise(text: str) -> str:
    """Collapse every whitespace run (NBSP included) to one space."""
    return _WS_RUN_RE.sub(" ", text or "").strip()


def fragment_tail(text: str, limit: int = FRAGMENT_TAIL_CHARS) -> str:
    """The trailing, whitespace-normalised slice of a pasted payload."""
    return _normalise(text)[-limit:]


def composer_holds_fragment(pane: str, fragment: str) -> bool:
    """True iff ``fragment`` is sitting in the LIVE compose box, unsent.

    Scoped from the bottom-most compose marker downwards, which is what
    separates "still in the composer" from "already submitted": a Codex
    pane renders a SUBMITTED message into the transcript with its own
    "›" marker and then shows the composer below it, so an unscoped
    substring test would report a delivered message as forever pending.

    Why a fragment at all. :func:`_compose_pending_live` recognises only
    Claude's "❯" box, so on a Codex pane it answers False no matter what
    is in the composer, and :func:`verify_submit_by_advancement` reads
    that as "nothing to submit" and returns True. Measured on
    handyman-01 (2026-09-05 11:31 UTC): `sac agents deliver` reported
    "DELIVERED and SUBMITTED", exit 0, while the payload sat in the
    Codex composer unsent — a single Enter by hand then started the
    turn. A caller that knows what it pasted can say so, and the check
    stops depending on which TUI drew the box.
    """
    fragment = _normalise(fragment)
    if not fragment:
        return False
    rows = (pane or "").splitlines()
    for index in range(len(rows) - 1, -1, -1):
        if any(marker in rows[index] for marker in _COMPOSE_MARKERS):
            return fragment in _normalise(" ".join(rows[index:]))
    return False


def _pane_is_input_idle(pane: str) -> bool:
    """True when the pane is safe to submit an Enter into.

    Input-idle = NOT busy (no spinner / "thinking" word / "esc to
    interrupt" line — reusing :func:`liveness_probe.pane_is_busy`, the
    fleet's single source of truth for the busy-marker list) AND the
    compose input is actually present: either claude's idle ready cue
    (:func:`prompts.is_ready`) OR a pasted-but-unsent buffer
    (:func:`prompts.detect` == ``compose-pending-unsent``) is on screen.

    The boot Enter-drop (sac-tui-enter-drop-on-boot) is precisely a
    submit fired while ``pane_is_busy`` is True (Claude initializing /
    spinner up / MCP mid-reconnect); the Ink TUI eats Enter in that
    window. Gating every send on this predicate is the structural fix.
    """
    # Local imports keep this module free of an import cycle and let the
    # detectors stay the fleet SSOT (no copy to desync).
    from .._lifecycle.liveness_probe import pane_is_busy
    from . import prompts as _prompts

    if pane_is_busy(pane):
        return False
    return _prompts.is_ready(pane) or _compose_pending_live(pane)


def is_fresh_boot_welcome_screen(pane: str) -> bool:
    """True iff a first-launch welcome/model-info/promo screen is on screen.

    Distinguishes a FRESH boot's ``Welcome to Claude Code!`` header box from
    a RESUMED session's ``Welcome back <name>!`` variant — both render the
    same boxed ``Claude Code v<version>`` banner (real capture:
    ``_V2_READY_PANE``, ``test_tui_session_v2_ready_marker.py``), differing
    only in the greeting.

    Card ``sac-tui-clear-compose-buffer-on-boot``: on a FRESH boot (no prior
    session, ``--continue`` omitted) this banner — plus any promo line under
    it, e.g. "Fable 5 is included in your weekly limit" — can still be up,
    with the Ink TUI's input not yet bound, when :func:`clear_compose_buffer`
    runs. Escape sent into that window does not reliably reach (or clear)
    the real compose box, so its "did NOT clear after N attempts" failure
    fires even though there is nothing genuine to clear yet. RESUMED
    sessions say "Welcome back" instead and never match here — the
    already-working resumed boot path is untouched.

    Scoped to :func:`_pane_tail` (the LIVE region), mirroring why
    ``prompts.detect`` scopes to its own tail (card
    ``sac-tui-stray-1-submitted-on-boot`, PR #598): once this banner has
    scrolled out of the live viewport it must stop matching.
    """
    tail = _pane_tail(pane)
    return "Claude Code v" in tail and "Welcome back" not in tail


def _wait_for_welcome_screen_to_clear(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    max_wait_s: float,
    poll_s: float,
    sleep_fn: Callable[[float], None],
    time_fn: Callable[[], float],
) -> str:
    """Poll until :func:`is_fresh_boot_welcome_screen` is False, bounded by
    ``max_wait_s``. Returns the last captured pane either way — fail-soft,
    mirroring :func:`_tui_drain.wait_for_settle`: the caller acts on
    whatever the pane shows once the bound is hit, it never blocks forever.
    """
    deadline = time_fn() + max_wait_s
    pane = capture_fn(name)
    while is_fresh_boot_welcome_screen(pane) and time_fn() < deadline:
        if poll_s > 0:
            sleep_fn(poll_s)
        pane = capture_fn(name)
    return pane


def clear_compose_buffer(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    send_keys_fn: Callable[[str], None],
    max_attempts: int = 5,
    poll_s: float = 0.4,
    welcome_wait_s: float = 2.5,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Empty any stale pasted-but-unsent text in the live compose box.

    Pure + fully injectable — no tmux import (mirrors
    :func:`verify_submit_by_advancement`). The fix for
    /compact-burst-on-restart (card sac-tui-clear-compose-buffer-on-boot):
    a persistent tmux pane carries EXTERNAL "compose-pending-unsent" input
    (e.g. a burst of stale ``/compact`` slash-commands the operator typed
    while the pane was busy) ACROSS an agent restart. If the boot's
    startup-prompt Enter fires with that stack still in the buffer, the whole
    stale stack is submitted. Calling this at the TOP of startup-prompt
    injection (before any paste/submit) drops the stale text first.

    Algorithm:

      1. **Capture once.** If the LIVE compose box is already empty
         (:func:`_compose_pending_live` False) there is nothing to clear —
         return ``True`` immediately. This is the COMMON case (a fresh boot
         with no stale buffer), so the no-op costs one capture and no
         keystroke.
      2. Otherwise send the verified CLEAR keystrokes
         (:data:`_COMPOSE_CLEAR_KEYS` = double ``Escape`` — empties a
         multi-line buffer without submitting or quitting), then poll
         ``capture_fn`` until the live box is empty, bounded by
         ``max_attempts``. Resend the clear keys each attempt (the Ink TUI
         can drop a keystroke mid-render).
      3. On success return ``True``; on exhaustion log LOUD (error with the
         pane tail + ``tmux attach`` hint) and return ``False`` but DO NOT
         raise — boot must still proceed (same fail-loud-not-fatal posture as
         the rest of the file). The downstream
         :func:`verify_submit_by_advancement` is the second safety net.

    **BUG 1 guard (Esc-cancel):** the CLEAR keystrokes are ``Escape`` — but
    while a modal whose dismissal treats Esc as CANCEL is on screen (the
    ``--dangerously-load-development-channels`` confirmation footer "Esc to
    cancel"), an ``Escape`` CANCELS the launch → claude exits → the tmux
    session DIES mid-boot (observed: ``^[^[^[^[`` then the session is gone).
    So BEFORE sending any ``Escape`` — on the initial capture AND before every
    resend — we re-check :func:`prompts.has_esc_cancel_modal`. When such a modal
    is present we REFUSE to Esc (log LOUD, return ``False``): the dev-channels
    modal must be dismissed by the modal drainer (Enter to confirm option 1)
    FIRST; the compose clear runs only once no cancelable modal remains.

    **BUG 3 guard (fresh-boot welcome screen):** on a FRESH boot (no prior
    session, ``--continue`` omitted) the first-launch welcome/model-info
    screen — see :func:`is_fresh_boot_welcome_screen` — can still be up, with
    the Ink TUI's input not yet bound, when this runs. ``Escape`` sent into
    that window is not reliably delivered to (or cleared from) the real
    compose box, so the resend loop below would exhaust ``max_attempts``
    against a screen it can never actually clear (card
    ``sac-tui-clear-compose-buffer-on-boot``, reproduced live 2026-07-05 /
    2026-07-08: "did NOT clear after 5 attempts" against a pane showing the
    welcome banner + a "Fable 5 is included in your weekly limit" promo
    line). So BEFORE the pending check — on the initial capture AND before
    every resend — we wait (bounded by ``welcome_wait_s``) for that screen to
    clear via :func:`_wait_for_welcome_screen_to_clear`, then re-evaluate the
    ACTUAL live box once it is gone. RESUMED sessions never show this banner
    (they render "Welcome back" instead), so this adds no latency there.
    """
    import logging

    from . import prompts as _prompts

    log = logging.getLogger(__name__)

    pane = capture_fn(name)
    if _prompts.has_esc_cancel_modal(pane):
        # A dev-channels / "Esc to cancel" modal is up: an Escape here would
        # CANCEL the launch and kill the session. Refuse to clear now — the
        # modal drainer must dismiss it (Enter → option 1) first.
        log_pane_fault(
            log,
            name,
            pane,
            "TuiSessionRuntime: REFUSING compose-buffer clear for %s — a "
            "cancelable modal ('Esc to cancel', e.g. dev-channels) is on "
            "screen; sending Escape would CANCEL the launch and kill the "
            "session. The modal drainer must dismiss it (Enter to confirm) "
            "before any Escape-based clear. Attach to inspect: "
            "`tmux attach -t %s`.",
            name,
            name,
        )
        return False
    if is_fresh_boot_welcome_screen(pane):
        # BUG 3 — the fresh-boot welcome/model-info/promo screen is up and
        # the Ink TUI's input is not reliably bound yet; wait it out before
        # trusting any read of the live compose box.
        pane = _wait_for_welcome_screen_to_clear(
            name,
            capture_fn=capture_fn,
            max_wait_s=welcome_wait_s,
            poll_s=poll_s,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
        )
    if not _compose_pending_live(pane):
        # Common case: nothing stale in the live box — no-op.
        return True

    last_pane = pane
    for _ in range(max_attempts):
        # Re-check before EVERY resend: a modal may have (re)appeared between
        # attempts, and an Escape into it would cancel/kill the session.
        current = capture_fn(name)
        if _prompts.has_esc_cancel_modal(current):
            log_pane_fault(
                log,
                name,
                current,
                "TuiSessionRuntime: aborting compose-buffer clear for %s "
                "mid-loop — a cancelable modal appeared; Escape would kill the "
                "session. Let the modal drainer dismiss it first.",
                name,
            )
            return False
        if is_fresh_boot_welcome_screen(current):
            # BUG 3 — (re)appeared mid-loop (a slow mount can outlast the
            # pre-loop wait). Skip this attempt WITHOUT sending Escape — it
            # cannot land on this screen — and let the next iteration's
            # capture see whether it has cleared by then.
            last_pane = current
            if poll_s > 0:
                sleep_fn(poll_s)
            continue
        for key in _COMPOSE_CLEAR_KEYS:
            send_keys_fn(key)
        # Let the clear render, then re-capture and verify the live box is empty.
        if poll_s > 0:
            sleep_fn(poll_s)
        last_pane = capture_fn(name)
        if not _compose_pending_live(last_pane):
            return True

    if is_fresh_boot_welcome_screen(last_pane):
        # Distinct from the generic exhaustion message below: we withheld
        # every Escape send (BUG 3 guard), so it would be misleading to blame
        # "the Ink TUI kept dropping the clear keystroke" — no keystroke was
        # ever sent. The welcome screen itself never released within budget.
        log_pane_fault(
            log,
            name,
            last_pane,
            "TuiSessionRuntime: compose-buffer clear for %s gave up after "
            "%d attempts — the fresh-boot welcome/model-info screen never "
            "cleared within the wait budget, so no Escape was sent at all "
            "(sending it would not have landed). Boot will proceed "
            "(verify_submit_by_advancement is the next net). Attach to "
            "inspect: `tmux attach -t %s`.",
            name,
            max_attempts,
            name,
        )
        return False

    log_pane_fault(
        log,
        name,
        last_pane,
        "TuiSessionRuntime: stale compose buffer for %s did NOT clear after "
        "%d attempts of %r — the Ink TUI kept dropping the clear keystroke (or "
        "new text keeps arriving). Boot will proceed (verify_submit_by_advancement "
        "is the next net), but the boot Enter may submit stale text. Attach to "
        "inspect/recover: `tmux attach -t %s`.",
        name,
        max_attempts,
        list(_COMPOSE_CLEAR_KEYS),
        name,
    )
    return False


def verify_submit_by_advancement(
    name: str,
    *,
    capture_fn: Callable[[str], str],
    send_keys_fn: Callable[[str], None],
    pending_fragment: str | None = None,
    max_resends: int = 8,
    poll_s: float = 0.6,
    appear_timeout_s: float = 5.0,
    idle_wait_s: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Submit a pasted compose buffer, waiting for idle and verifying by
    buffer-advancement. Pure + fully injectable — no tmux import.

    This is the fix for the boot Enter-drop (task
    ``sac-tui-enter-drop-on-boot``). The OLD ``_verify_submitted`` resent
    Enter ``max_resends`` times back-to-back with only a fixed ``poll_s``
    sleep — but on (re)start ALL of those resends fire INSIDE the busy /
    initializing window (spinner ``Photosynthesizing…`` / ``Working…`` /
    ``Ruminating…`` up, MCP mid-reconnect), where the Ink TUI silently
    drops every Enter. The agent then sits with its startup_prompt pasted
    but unsent.

    Algorithm — wait-for-idle + verify-by-advancement, mirroring the
    proven emacs-claude-code TUI driver cadence (short text→Enter gap,
    send-verify by buffer advancement, anti-flicker re-capture):

      1. **Wait for the paste to RENDER** as ``compose-pending-unsent``
         (``capture_fn`` → :func:`prompts.detect`). A multi-line paste
         takes a beat. If it never renders within ``appear_timeout_s``
         the turn was either submitted instantly or there was nothing to
         submit → return ``True`` (nothing to force).
      2. For up to ``max_resends`` attempts:
         a. **Wait for input-idle** (:func:`_pane_is_input_idle`): no
            spinner AND the compose input present. Bounded by
            ``idle_wait_s``. While waiting, if the buffer ALREADY advanced
            (no longer ``compose-pending-unsent``) it was submitted →
            return ``True``. Do NOT send Enter while busy.
         b. **Send one Enter** (only once idle + still pending).
         c. **Verify advancement**: poll a short settle; if the buffer is
            no longer ``compose-pending-unsent`` the Enter landed →
            return ``True``.
         d. Otherwise adaptive back-off (settle grows each attempt) and
            retry — re-checking idle from scratch so the next Enter again
            only fires into a non-busy pane.
      3. On exhaustion → fail LOUD (error log with the pane tail +
         ``tmux attach`` guidance) and return ``False`` (never a silent
         give-up).

    ``pending_fragment`` is what the caller just pasted. Claude's "❯"
    box is still the primary signal; the fragment is consulted in
    ADDITION, for a pane whose composer :func:`_compose_pending_live`
    cannot see at all — a Codex pane, where the absence of "❯" made
    phase 1 conclude "nothing to submit" and return True over a payload
    that was sitting there unsent. Consulting both keeps Claude's
    behaviour byte-for-byte (its large pastes collapse to "[Pasted text
    #1 …]", so the fragment alone would not be visible) while giving
    Codex a signal that is true of its composer.

    Returns ``True`` if the buffer was observed to advance (or never
    rendered as pending), ``False`` if it stayed pending after all
    bounded attempts.
    """
    import logging

    log = logging.getLogger(__name__)

    def _advanced() -> str:
        """Capture once; return pane text. Buffer 'advanced' iff the
        returned text no longer detects as compose-pending-unsent."""
        return capture_fn(name)

    tail = fragment_tail(pending_fragment or "")

    def _pending(pane: str) -> bool:
        """Is the pasted turn still sitting in the live compose box?

        Claude's own box decides whenever it is on screen at all, so this
        is byte-for-byte the old behaviour for every Claude pane. The
        fragment is consulted ONLY where the marker test is structurally
        blind -- a pane that draws no Claude marker anywhere, which is
        exactly the Codex case that read as "nothing to submit".
        """
        if _compose_pending_live(pane):
            return True
        if _CLAUDE_COMPOSE_MARKER in (pane or ""):
            return False
        return composer_holds_fragment(pane, tail)

    def _input_idle(pane: str) -> bool:
        """Safe to submit an Enter into?

        :func:`_pane_is_input_idle` proves the compose input is present by
        Claude's cues, so a Codex pane could never satisfy it and the Enter
        was never sent. A composer visibly holding OUR payload is the same
        proof, and it still has to pass the shared busy check.
        """
        if _pane_is_input_idle(pane):
            return True
        from .._lifecycle.liveness_probe import pane_is_busy

        return _pending(pane) and not pane_is_busy(pane)

    # Phase 1 — wait for the pasted text to appear as an unsent buffer.
    appear_deadline = time_fn() + appear_timeout_s
    saw_pending = False
    last_pane = ""
    while time_fn() < appear_deadline:
        last_pane = _advanced()
        if _pending(last_pane):
            saw_pending = True
            break
        if poll_s > 0:
            sleep_fn(poll_s)
    if not saw_pending:
        return True

    # Phase 2 — bounded resend loop: wait-for-idle, send Enter, verify.
    for attempt in range(max_resends):
        # 2a — wait for input-idle (no spinner), bounded by idle_wait_s.
        # Bail early on advancement (a prior Enter, or the operator,
        # already submitted) so we never send a stray Enter into the
        # next, empty prompt.
        idle_deadline = time_fn() + idle_wait_s
        idle = False
        while time_fn() < idle_deadline:
            last_pane = _advanced()
            if not _pending(last_pane):
                return True
            if _input_idle(last_pane):
                idle = True
                break
            if poll_s > 0:
                sleep_fn(poll_s)
        if not idle:
            # Still busy after the whole idle window — do NOT blind-fire
            # Enter (that is the original bug). Loop to the next attempt;
            # the final failure path below reports loudly if it persists.
            continue

        # 2b — idle + still pending: send exactly one Enter.
        send_keys_fn("Enter")

        # 2c — verify advancement with an adaptive settle. Anti-flicker:
        # re-capture after a short gap; the buffer clears within a frame
        # or two when the Enter lands.
        settle = poll_s * (1 + attempt)  # adaptive back-off between attempts
        verify_deadline = time_fn() + settle
        while True:
            if poll_s > 0:
                sleep_fn(poll_s)
            last_pane = _advanced()
            if not _pending(last_pane):
                return True
            if time_fn() >= verify_deadline:
                break
        # 2d — not advanced; loop to next attempt (re-checks idle).

    pane = capture_fn(name)
    log_pane_fault(
        log,
        name,
        pane or last_pane,
        "TuiSessionRuntime: startup_prompt for %s stayed pasted-but-UNSENT "
        "after %d wait-for-idle Enter attempts — the Ink TUI keeps dropping "
        "Enter (or the pane never left BUSY). Attach to inspect/recover: "
        "`tmux attach -t %s` then press Enter.",
        name,
        max_resends,
        name,
    )
    return False
