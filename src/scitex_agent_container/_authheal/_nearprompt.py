"""NEAR-PROMPT discriminator: is this agent's auth banner the CURRENT UI STATE?

WHY A SECOND DISCRIMINATOR EXISTS
    Both existing detectors decide "wedged" with a FREEZE test, and a freeze
    test asks the wrong question:

      * the deployed ``~/.scitex/agent-container/bin/auth-heal.py`` requires the
        banner to be present AND the volatile-stripped WHOLE-PANE signature to
        be byte-identical across two runs;
      * :func:`.._runners._tmux.auth_status.is_stuck` softens that to the
        banner's DISTANCE from the prompt, but still requires equality across
        two captures.

    Both therefore classify as HEALTHY an agent that IS wedged but whose pane
    still moves — a spinner whose frame is not in ``_VOLATILE_RE``, a clock, a
    rate-limit countdown, a redraw that reflows a line, anything that shifts a
    non-chrome line into or out of the gap. These are exactly the agents the
    operator then restarts BY HAND, and the deployed script recorded the miss in
    a cache NOTE field rather than in a log, so the miss was invisible.

    The freeze test was not arbitrary: it defends against an agent that merely
    QUOTES "Login expired" while discussing the incident, and that concern is
    REAL (verified live 2026-07-12: 3 of 4 flagged agents were prose). This
    module keeps that defence and drops the freeze, by asking a question that
    separates the two cases directly:

        Is the banner the CURRENT UI STATE — pinned in the conversation tail
        directly above the input prompt — or is it TEXT IN SCROLLBACK?

    A wedged agent's banner is the last thing its TUI rendered, so it sits in
    the tail whether or not the pane animates. A quoting agent kept working
    after it said the words, so its own later output pushed the quote up out of
    the tail. That is a property of ONE capture, so an animating-but-wedged
    agent is caught — which is the whole point.

REUSE, NOT REINVENTION
    The near-prompt geometry is NOT re-derived here. :func:`probe_pane` (the
    tested, NBSP-aware, chrome-stripping matcher behind ``sac agents
    auth-status``) already computes exactly it; this module consumes
    ``AuthProbe.present`` and adds only the verdict + the plain-words WHY.
    ``is_stuck`` — the freeze leg — is deliberately NOT used.

TRI-STATE, ALWAYS
    A pane that could not be read, or one with no locatable prompt line, is
    UNKNOWN: we did not learn where the current UI state ENDS, so we cannot say
    the banner is or is not in it. UNKNOWN is never restarted (absence of
    evidence is not evidence of a wedge) and never counted healthy — it is
    logged, with its reason, as the finding it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._runners._tmux.auth_status import banner_kind, probe_pane
from .._runners._tmux.prompts import is_ready

__all__ = [
    "VERDICT_LOGIN_REQUIRED",
    "VERDICT_OK",
    "VERDICT_UNKNOWN",
    "WHY_NEAR_PROMPT",
    "WHY_NO_BANNER",
    "WHY_NO_PROMPT_LINE",
    "WHY_PANE_UNREADABLE",
    "WHY_SCROLLBACK_ONLY",
    "Finding",
    "classify_pane",
    "classify_panes",
]

#: The banner is the current UI state → this agent needs a restart.
VERDICT_LOGIN_REQUIRED = "login_required"
#: We read the pane and the banner is NOT the current UI state.
VERDICT_OK = "ok"
#: We did not learn anything about this agent. Never restarted, never healthy.
VERDICT_UNKNOWN = "unknown"

#: The five reasons a verdict can be reached. Every one of them is written to
#: the log verbatim, because "why" is the field the deployed script threw away.
WHY_NEAR_PROMPT = "near-prompt"
WHY_SCROLLBACK_ONLY = "scrollback-only"
WHY_NO_BANNER = "no-banner"
WHY_PANE_UNREADABLE = "pane-unreadable"
WHY_NO_PROMPT_LINE = "no-prompt-line"


@dataclass(frozen=True)
class Finding:
    """One agent's verdict plus every observation that produced it.

    Everything here is written to the log. ``why`` is the short machine tag
    (:data:`WHY_NEAR_PROMPT` etc.) and ``detail`` the sentence a human reads;
    ``ready``, ``distance`` and ``banner`` are the raw evidence, kept so a
    verdict can be re-derived afterwards from the log alone rather than trusted.
    """

    agent: str
    verdict: str
    why: str
    detail: str
    banner: str | None = None
    distance: int | None = None
    prompt_found: bool = False
    ready: bool = False
    pane: str | None = None

    @property
    def login_required(self) -> bool:
        return self.verdict == VERDICT_LOGIN_REQUIRED

    def to_dict(self) -> dict:
        """Everything EXCEPT the raw pane, which the log writes as its own block."""
        return {
            "agent": self.agent,
            "verdict": self.verdict,
            "why": self.why,
            "detail": self.detail,
            "banner": self.banner,
            "distance": self.distance,
            "prompt_found": self.prompt_found,
            "ready": self.ready,
        }


def _scrollback_banner(pane: str) -> str | None:
    """The banner kind seen ANYWHERE on the pane, or ``None``.

    Used only to tell the two OK shapes apart in the log: an agent that quoted
    the phrase somewhere (:data:`WHY_SCROLLBACK_ONLY`) versus one with no auth
    text at all (:data:`WHY_NO_BANNER`). The distinction changes no decision —
    both are OK — but it is the difference between a log that says "we saw the
    words and ruled them out" and one that says "we saw nothing", and only the
    first lets someone audit a miss afterwards.
    """
    for line in pane.splitlines():
        kind = banner_kind(line)
        if kind is not None:
            return kind
    return None


def classify_pane(agent: str, pane: str | None) -> Finding:
    """Classify ONE captured pane. Pure: no tmux, no I/O, no cross-run state.

    The absence of cross-run state IS the fix. Taking the whole decision from a
    single capture is what makes an animating pane classifiable at all, because
    there is no second reading for the animation to disagree with.
    """
    if pane is None:
        return Finding(
            agent=agent,
            verdict=VERDICT_UNKNOWN,
            why=WHY_PANE_UNREADABLE,
            detail=(
                f"{agent}: the pane could NOT be captured, so nothing was "
                f"observed about its auth. NOT restarted (absence of evidence "
                f"is not evidence of a wedge) and NOT counted healthy"
            ),
        )

    probe = probe_pane(pane)
    ready = is_ready(pane)

    if not probe.prompt_found:
        # No prompt line ⇒ no anchor ⇒ we do not know where the current UI
        # state ends, so we cannot say the banner is in it OR out of it. That
        # is the definition of UNKNOWN, and calling it OK would be the same
        # false-green this module exists to end, just moved to another inch.
        return Finding(
            agent=agent,
            verdict=VERDICT_UNKNOWN,
            why=WHY_NO_PROMPT_LINE,
            detail=(
                f"{agent}: no input-prompt line was found on the pane, so the "
                f"near-prompt tail has no anchor and the banner's position "
                f"could not be judged. NOT restarted and NOT counted healthy"
            ),
            prompt_found=False,
            ready=ready,
            pane=pane,
        )

    if probe.present:
        return Finding(
            agent=agent,
            verdict=VERDICT_LOGIN_REQUIRED,
            why=WHY_NEAR_PROMPT,
            detail=(
                f"{agent}: the system auth banner {probe.banner!r} is the "
                f"CURRENT UI STATE — pinned {probe.distance} non-chrome line(s) "
                f"above the input prompt, inside the near-prompt tail. This is "
                f"a rendered banner, not scrollback text, so the agent is "
                f"wedged whether or not its pane is animating"
            ),
            banner=probe.banner,
            distance=probe.distance,
            prompt_found=True,
            ready=ready,
            pane=pane,
        )

    quoted = _scrollback_banner(pane)
    if quoted is not None:
        return Finding(
            agent=agent,
            verdict=VERDICT_OK,
            why=WHY_SCROLLBACK_ONLY,
            detail=(
                f"{agent}: the phrase {quoted!r} appears on the pane but ABOVE "
                f"the near-prompt tail — the agent produced output after it, so "
                f"it is scrollback text (quoting/discussing the incident), not "
                f"the current UI state. NOT restarted"
            ),
            banner=quoted,
            prompt_found=True,
            ready=ready,
            pane=pane,
        )

    return Finding(
        agent=agent,
        verdict=VERDICT_OK,
        why=WHY_NO_BANNER,
        detail=f"{agent}: no system auth banner anywhere on the pane",
        prompt_found=True,
        ready=ready,
        pane=pane,
    )


def classify_panes(panes: dict) -> tuple[Finding, ...]:
    """Classify ``{agent: pane_or_None}``, sorted by agent name for stable logs."""
    return tuple(classify_pane(name, panes[name]) for name in sorted(panes))
