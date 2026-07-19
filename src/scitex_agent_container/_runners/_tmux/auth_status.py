"""Prompt-anchored TUI auth-banner detector (near-prompt + distance-frozen).

WHY THIS EXISTS
    The TUI watchdog is the SOLE safety net for the 2026-07-11 auth-death
    class — a stale in-memory OAuth token that only a restart clears (see
    ``_runners/_auth_failure``). It must SEE the ``tui-<agent>`` fleet AND
    tell a REAL "Login expired * Please run /login" banner apart from an
    agent merely QUOTING that phrase while it discusses the incident.

    The naive matcher (``_state/_meta/pane._classify_pane_state``) flags the
    phrase ANYWHERE on the pane, so an agent replying about the incident
    false-positives (verified live 2026-07-12: 3 of 4 flagged agents were
    prose, 1 was the real frozen ``scitex-hpc``). This module is the
    hardened replacement — it separates REAL from PROSE with two
    prompt-anchored signals:

      1. NEAR-PROMPT — a banner counts only when it sits in the conversation
         TAIL: the last :data:`TAIL_LINES` non-chrome lines directly above
         the input-prompt line (located via
         :func:`prompts.prompt_line_index`). A banner higher in scrollback —
         where a quoting agent's own later output pushes it — is ignored.
      2. DISTANCE-FROZEN — the banner's DISTANCE from the prompt (count of
         non-chrome lines between it and the prompt) is tracked across runs
         in caller-held LOCAL STATE. A REAL wedged agent is frozen: the same
         banner kind at the same distance on two consecutive captures. A
         working/prose agent produces output, so the distance CHANGES (or the
         banner leaves the tail) — never frozen.

    Both signals REUSE the tested, NBSP-aware prompt detector in
    ``prompts.py`` rather than re-deriving the prompt glyph. Everything here
    is a pure function of pane text + a small state dict — no tmux, no I/O —
    so it is unit-testable against captured panes without mocks. Live capture
    + ``tui-`` session enumeration live in the ``sac agents auth-status`` CLI
    command (``cli_pkg/_auth_status``).

MAINTENANCE
    The banner strings + chrome patterns below mirror what Anthropic's Ink
    TUI renders, which changes between releases. When a real banner stops
    being flagged (or prose starts being flagged), update ``_AUTH_STARTS`` /
    ``_VOLATILE_RE`` here and add the captured pane as a fixture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .prompts import normalize_tui_whitespace, prompt_line_index

__all__ = [
    "AuthProbe",
    "TAIL_LINES",
    "banner_kind",
    "evaluate",
    "is_stuck",
    "probe_pane",
    "probe_to_state",
]

# Near-prompt window: a banner is judged REAL only when it is among the last N
# non-chrome conversation lines directly above the input prompt. ~6 mirrors the
# operator's "last ~6 non-chrome lines" refinement (TG 1507, 2026-07-12).
TAIL_LINES = 6

# Left-side TUI decoration stripped before a line is matched, so a rendered
# result like "  |_  Login expired * Please run /login" is judged on the text
# after the marker. The result/spinner/quote glyphs come verbatim from the
# proven dotfiles matcher; `` `` (NBSP) is included because Claude's Ink
# TUI renders the gap after the "⎿" result marker as a NBSP, not an ASCII
# space — a real 2026-07-11 capture reads ``⎿  Please run /login``, so
# leaving NBSP out of the strip set left the text starting with " Please"
# and the banner went undetected (regression-guarded by the head-mba fixture).
_MARKERS = " \t" + chr(0xA0) + "⎿✻●·│┃─⏺└╭>❯'\"`|*-"

# A line is a SYSTEM auth banner when, after its left decoration is stripped,
# it STARTS WITH one of these canonical Claude-rendered auth phrases. Anchoring
# on the START (not a bare substring) is what rejects prose: an agent line like
# '* figrecipe died in a "Login expired" loop' strips to "figrecipe died ...",
# which starts with none of these. (The near-prompt + distance layers handle
# the harder case — a VERBATIM banner quote — since a quote sits up in
# scrollback and the pane keeps moving.)
_AUTH_STARTS = (
    "Not logged in",
    "Login expired",
    "Session expired",
    "Please run /login",
    "Please re-run /login",
    "Invalid API key",
    "Invalid authentication credentials",
    "OAuth token has expired",
    "OAuth token expired",
    "Authentication failed",
    "Credit balance is too low",
)

# Standalone HTTP auth-rejection line (401 Unauthorized / 403 Forbidden). 429
# (rate limit) is deliberately excluded — a restart does not fix a rate wall.
_API_AUTH_RE = re.compile(r"^API Error:\s*(?:401|403)\b")

# Structural chrome: a horizontal box-drawing separator (the rule below the
# conversation / above the prompt box). Whole line is box-drawing + whitespace.
_SEPARATOR_RE = re.compile(r"^[─-╿\s]+$")

# Volatile status chrome — lines whose text changes even when NOTHING new
# happens (gauges, elapsed clocks, spinners, the 0s-turn "Sauteed for 0s"
# marker of a wedged /loop agent, token counters, rotating tips, the status
# bar). Excluded from the conversation tail + the distance count so a cosmetic
# spinner tick is never mistaken for the agent producing real output.
_VOLATILE_RE = re.compile(
    r"Usage\s|Context\s"  # full gauge rows
    r"|ctx:\s*\d+%|\b\d+h:\s*\d+%"  # compact status bar
    r"|\(\s*\d+h\s*\d+m|\(\s*\d+m\s*\d+s|\(\s*\d+s\b"  # elapsed clocks
    r"|\bfor \d+s\b"  # "* Sauteed for 0s" 0s-turn spinner
    r"|esc to interrupt|ctrl\+[a-z]"
    r"|Running scheduled task"
    r"|tokens\)|Tip:|bypass permissions"
)


def _strip_markers(line: str) -> str:
    """Drop left TUI decoration + trailing whitespace from a captured line.

    ``_MARKERS`` includes the NBSP (U+00A0) that Claude's Ink TUI renders as
    the gap after the ``⎿`` result marker, so ``⎿  Please run /login``
    strips cleanly to ``Please run /login ...`` (see ``_MARKERS`` note).
    """
    return line.lstrip(_MARKERS).rstrip()


def banner_kind(line: str) -> str | None:
    """Normalised auth-banner identity for ``line``, or ``None`` if not a banner.

    Returns the matched canonical phrase (e.g. ``"Login expired"``) or
    ``"API Error: 4xx"`` — deliberately NORMALISED, not the raw line, so a
    volatile ``request_id`` / timestamp / 401 JSON body embedded in the banner
    does not defeat the cross-run frozen comparison (a wedged ``/loop`` agent
    re-renders the SAME banner with a NEW request id every wakeup).

    Unicode whitespace is normalised BEFORE the marker strip (see
    :func:`prompts.normalize_tui_whitespace`). Order matters: ``_MARKERS``
    strips a leading NBSP but NOT the other Unicode spaces, so normalising
    first is what lets an exotic space in the LEFT DECORATION be stripped at
    all. Normalising the phrase side too keeps the comparison symmetric, so a
    stray NBSP pasted into ``_AUTH_STARTS`` cannot silently stop matching.
    """
    s = _strip_markers(normalize_tui_whitespace(line))
    for phrase in _AUTH_STARTS:
        if s.startswith(normalize_tui_whitespace(phrase)):
            return phrase
    if _API_AUTH_RE.match(s):
        return "API Error: 4xx"
    return None


def _is_chrome(line: str) -> bool:
    """True for a blank line, a separator rule, or a volatile status line."""
    s = line.strip()
    if not s:
        return True
    if _SEPARATOR_RE.match(s):
        return True
    return bool(_VOLATILE_RE.search(line))


@dataclass(frozen=True)
class AuthProbe:
    """One capture's verdict.

    ``prompt_found`` — an input-prompt line was located at all.
    ``present``      — a system auth banner sits in the near-prompt tail.
    ``distance``     — non-chrome lines between that banner and the prompt
                       (0 = directly above); ``None`` when no banner.
    ``banner``       — the normalised banner kind (see :func:`banner_kind`).
    """

    prompt_found: bool
    present: bool
    distance: int | None
    banner: str | None


def probe_pane(pane: str) -> AuthProbe:
    """Probe one captured pane for a near-prompt auth banner.

    Anchors on the bottom-most prompt line, walks the non-chrome conversation
    lines above it, and reports the banner NEAREST the prompt within the last
    :data:`TAIL_LINES` of them (if any) plus its distance from the prompt.
    """
    lines = pane.splitlines()
    p = prompt_line_index(pane)
    if p is None:
        return AuthProbe(prompt_found=False, present=False, distance=None, banner=None)
    # Non-chrome conversation lines strictly above the prompt, keeping their
    # original indices so distance is measured in real screen lines.
    convo = [(i, ln) for i, ln in enumerate(lines[:p]) if not _is_chrome(ln)]
    tail = convo[-TAIL_LINES:]
    banner_at: int | None = None
    banner: str | None = None
    for i, ln in tail:  # ascending index → last hit is NEAREST the prompt
        kind = banner_kind(ln)
        if kind is not None:
            banner_at, banner = i, kind
    if banner_at is None:
        return AuthProbe(prompt_found=True, present=False, distance=None, banner=None)
    distance = sum(1 for (j, _ln) in convo if j > banner_at)
    return AuthProbe(prompt_found=True, present=True, distance=distance, banner=banner)


def probe_to_state(probe: AuthProbe) -> dict:
    """Serialise a probe to the minimal dict a caller persists between runs."""
    return {
        "present": probe.present,
        "distance": probe.distance,
        "banner": probe.banner,
    }


def is_stuck(probe: AuthProbe, prev: dict | None) -> bool:
    """Frozen decision: banner present now AND unchanged since the previous run.

    STUCK requires a prior state (never fires on first sight) whose banner was
    present at the SAME distance and of the SAME kind. A changed distance —
    the agent produced output — or an absent/renamed banner is NOT stuck.
    """
    if not probe.present or probe.distance is None:
        return False
    if not prev or not prev.get("present"):
        return False
    return prev.get("distance") == probe.distance and prev.get("banner") == probe.banner


def evaluate(pane: str | None, prev: dict | None) -> tuple[AuthProbe, bool]:
    """``(probe, stuck)`` for a capture and the previous run's state dict.

    An uncapturable pane (``None``) is the clear, never-stuck result — the
    honest "could not read" outcome, never a false LOGIN-REQUIRED.
    """
    if pane is None:
        return (
            AuthProbe(prompt_found=False, present=False, distance=None, banner=None),
            False,
        )
    probe = probe_pane(pane)
    return probe, is_stuck(probe, prev)
