"""POSITIONAL auth detection: a banner ABOVE the startup marker is HISTORY.

THE OPERATOR'S RULE
    After a restart, sac injects its startup prompt into the pane. That text is
    a DELIMITER between two eras of the pane:

        banner ABOVE the last startup marker -> printed BEFORE this boot. HISTORY.
        banner BELOW the last startup marker -> printed BY this boot. CURRENT.

    Matching the banner textually — anywhere on the pane, or even anchored near
    the prompt — cannot tell those apart, because the pane keeps rendering the
    old banner long after the agent recovered from it.

WHY EVERY TEXTUAL RULE FAILED, INCLUDING THE HARDENED ONES
    Verified on the real captured pane in
    ``tests/.../specimen_grant_20260718_alive_false_positive.log``. grant was
    ALIVE — it answered a ping, read files, ran commands and finished a
    background publish — and:

      * ``auth-heal``'s detector calls it STUCK. It matches a banner ANYWHERE on
        the visible screen with no positional anchor at all.
      * sac's ``auth-status`` calls it AUTH-FAILED. It requires the banner in
        the last few non-chrome lines above the prompt — which it genuinely was.

    Both are correct by their own rules and both are WRONG about reality,
    because a trailing banner means "the last thing rendered was a banner", not
    "this agent is broken now". An agent that 401'd, recovered, and went idle
    renders nothing further, so the banner stays trailing forever.

    The "frozen across two runs" hardening makes this WORSE. An idle agent's
    visible pane does not change, so a stale banner on a healthy idle agent
    looks maximally frozen. Freeze corroborates IDLENESS — the one property the
    wedged agent and the recovered-idle agent share — and so reports the
    confusion with high confidence.

A CAVEAT THAT KEEPS THE RESTART ARM OFF
    On the ONE real specimen available, the startup marker renders inside the
    COMPOSER (the input box), below the prompt glyph and above the status bar.
    Nothing can ever appear below it there, so the predicate returns ALIVE for
    structural reasons as well as correct ones. It gets the right answer on the
    only true negative we have, and that answer does not prove it would ever say
    DEAD. There is NO captured true-positive pane (banner below the marker), so
    the DEAD branch is UNEXERCISED against production rendering, and the restart
    arm stays disabled until one exists. Inferring the positive case from the
    negative one is how a detector that has never said yes ships as if it had.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._runners._tmux.auth_status import banner_kind

__all__ = [
    "ALIVE",
    "DEAD",
    "UNKNOWN",
    "STARTUP_MARKER",
    "PositionalVerdict",
    "classify_positional",
]

#: ALIVE / DEAD / UNKNOWN. Only DEAD may ever authorise an action, and DEAD is
#: reachable only from a banner rendered BELOW the marker by THIS boot.
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

#: The delimiter. Taken from the SAME constant the start path injects
#: (``config._loaders.DEFAULT_STARTUP_PROMPT``) rather than a copied string, so
#: a change to the boot kick cannot silently desynchronise the detector from the
#: thing it anchors on. Only the stable leading sentence is matched: the rest of
#: the prompt wraps across pane lines at a width we do not control.
STARTUP_MARKER = "Start or continue."


@dataclass(frozen=True)
class PositionalVerdict:
    """What the pane's LAYOUT established, with the evidence that established it."""

    agent: str
    state: str
    detail: str
    marker_line: int | None = None
    banner_lines: tuple[int, ...] = ()
    banners_below: tuple[int, ...] = ()

    @property
    def may_restart(self) -> bool:
        """Only a corroborated DEAD, and only once a true positive exists.

        Hard-wired False. The DEAD branch has never been exercised against a
        real captured pane, so nothing here is allowed to authorise a
        destructive action yet — a restart kills live work, and this detector's
        two predecessors both destroyed healthy agents while being certain.
        """
        return False


def _marker_line(lines: list[str]) -> int | None:
    """Index of the LAST startup marker, or ``None``.

    Last, not first: an agent restarted several times carries several markers,
    and only the most recent one delimits the CURRENT boot.
    """
    found = None
    for i, line in enumerate(lines):
        if STARTUP_MARKER in line:
            found = i
    return found


def classify_positional(agent: str, pane: str | None) -> PositionalVerdict:
    """Classify one VISIBLE pane capture by banner position. Pure, no I/O.

    The pane must be captured WITHOUT ``-S`` (visible screen only). Scrollback
    would reintroduce arbitrarily old banners whose era cannot be established.
    """
    if pane is None:
        return PositionalVerdict(
            agent=agent,
            state=UNKNOWN,
            detail=(
                f"{agent}: the pane could not be captured. UNKNOWN — nothing was "
                f"observed, and nothing may be done"
            ),
        )

    lines = pane.splitlines()
    banners = tuple(i for i, line in enumerate(lines) if banner_kind(line) is not None)
    marker = _marker_line(lines)

    if marker is None:
        # No delimiter ⇒ no way to date any banner we can see. This is the
        # branch that must never be optimised into a verdict: not finding our
        # anchor tells us about OUR reading, not about the agent.
        return PositionalVerdict(
            agent=agent,
            state=UNKNOWN,
            detail=(
                f"{agent}: no startup marker ({STARTUP_MARKER!r}) is on the "
                f"visible pane, so no banner can be dated as before-or-after "
                f"this boot. UNKNOWN — never restarted. "
                f"{len(banners)} banner line(s) seen and deliberately ignored"
            ),
            banner_lines=banners,
        )

    below = tuple(i for i in banners if i > marker)

    if not below:
        return PositionalVerdict(
            agent=agent,
            state=ALIVE,
            detail=(
                f"{agent}: {len(banners)} auth banner(s) at line(s) "
                f"{list(banners)}, ALL above the startup marker at line "
                f"{marker} — they were printed before this boot and are "
                f"HISTORY. Nothing this boot rendered says it cannot "
                f"authenticate. ALIVE; do not touch it"
            ),
            marker_line=marker,
            banner_lines=banners,
        )

    return PositionalVerdict(
        agent=agent,
        state=DEAD,
        detail=(
            f"{agent}: an auth banner is rendered at line(s) {list(below)}, "
            f"BELOW the startup marker at line {marker} — printed by THIS boot, "
            f"so it is the current state and not history"
        ),
        marker_line=marker,
        banner_lines=banners,
        banners_below=below,
    )
