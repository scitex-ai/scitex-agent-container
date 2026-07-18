"""Corroborate DEADNESS positively — a trailing banner is not evidence of it.

THE FALSE POSITIVE THIS EXISTS FOR (live, 2026-07-18)
    ``sac agents auth-status`` reported ``grant AUTH-FAILED / revoked / "Login
    expired"``. grant was ALIVE: it answered a ping, read files, ran shell
    commands and finished a background publish task, at Context 86%.

    The mechanism is NOT what it first looked like. Both detectors capture the
    VISIBLE SCREEN ONLY (``capture-pane -p``, no ``-S``), and sac's additionally
    requires the banner to sit in the last few non-chrome lines above the
    prompt. Neither reads scrollback. The real defect is subtler and worse:

        A TRAILING BANNER MEANS "the last thing this agent RENDERED was a
        banner". It does not mean "this agent is broken NOW".

    An agent that hit a 401, printed the banner, recovered, and then went IDLE
    renders nothing further — so the banner stays trailing forever. On grant's
    captured pane the banner really was the last non-chrome line above the
    prompt. The detector fired correctly by its own rule and was still wrong
    about reality.

WHY "FROZEN ACROSS TWO RUNS" MAKES IT WORSE, NOT BETTER
    The freeze test was added to fight false positives, and it inverts. An IDLE
    agent's visible pane does not change between two captures, so a stale banner
    on an idle-but-healthy agent looks MAXIMALLY frozen. Freeze corroborates
    IDLENESS, which is the one property the wedged agent and the recovered-idle
    agent share. It cannot separate them, and it reports the confusion with high
    confidence.

WHAT ACTUALLY SEPARATES THEM
    Only a POSITIVE observation of life. A restart is destructive — it kills
    whatever the agent is doing — so it must be justified by evidence of
    deadness, never by the absence of evidence of health.

      * pane CHANGED between two captures  -> ALIVE. Something is rendering.
      * pane UNCHANGED over a long window  -> still only a CANDIDATE. Idle and
        wedged are indistinguishable from the outside; this is the state that
        needs a ping before anyone touches it.
      * pane unreadable                    -> UNKNOWN.

    The window must exceed the SLOWEST hook in the loop. A probe that concluded
    "dead" 25 seconds after a ping was photographing the moment before the
    reply — that agent's UserPromptSubmit hook alone times out at 30s. Budget
    60s+, and treat a non-answer at T as UNKNOWN, never as DEAD.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_OBSERVE_S",
    "MIN_OBSERVE_S",
    "Liveness",
    "LIVE",
    "IDLE_CANDIDATE",
    "UNKNOWN",
    "corroborate",
]

#: A pane that changed is alive. A pane that did not is merely idle-or-wedged,
#: and telling those apart needs a ping — never an assumption.
LIVE = "alive"
IDLE_CANDIDATE = "idle-candidate"
UNKNOWN = "unknown"

#: Floor for the observation window, in seconds. Below this the answer is not
#: measurement, it is a photograph taken before the subject could reply: an
#: agent's UserPromptSubmit hook alone can take 30s.
MIN_OBSERVE_S = 60.0

#: Default window. Long on purpose — the cost of waiting is a slower pass; the
#: cost of deciding early is restarting an agent that was about to answer.
DEFAULT_OBSERVE_S = 90.0


@dataclass(frozen=True)
class Liveness:
    """What two captures, a window apart, actually established.

    ``state`` is one of :data:`LIVE` / :data:`IDLE_CANDIDATE` / :data:`UNKNOWN`.
    There is deliberately no ``DEAD``: two pane captures cannot produce one.
    The strongest negative available from watching a screen is "nothing
    happened", and nothing-happened is what a healthy idle agent looks like.
    """

    agent: str
    state: str
    detail: str
    changed: bool = False
    observed_s: float = 0.0

    @property
    def may_restart(self) -> bool:
        """NEVER true from pane observation alone.

        Kept as an explicit property so a future caller has to change this
        function — and read the reasoning above — rather than quietly treating
        :data:`IDLE_CANDIDATE` as permission.
        """
        return False


def corroborate(
    agent: str,
    pane_before: str | None,
    pane_after: str | None,
    *,
    observed_s: float,
) -> Liveness:
    """Classify two captures of one agent, ``observed_s`` apart. Pure function.

    An unreadable capture on EITHER side is UNKNOWN: a comparison needs two
    readings, and inventing one is how a blind probe starts returning verdicts.
    """
    if pane_before is None or pane_after is None:
        which = "before" if pane_before is None else "after"
        return Liveness(
            agent=agent,
            state=UNKNOWN,
            detail=(
                f"{agent}: the '{which}' pane could not be captured, so no "
                f"comparison was possible. UNKNOWN — never restarted"
            ),
            observed_s=observed_s,
        )

    if observed_s < MIN_OBSERVE_S:
        return Liveness(
            agent=agent,
            state=UNKNOWN,
            detail=(
                f"{agent}: observed for only {observed_s:.0f}s, below the "
                f"{MIN_OBSERVE_S:.0f}s floor — an agent's hooks alone can take "
                f"30s, so this window cannot tell a slow reply from no reply. "
                f"UNKNOWN, not a verdict"
            ),
            observed_s=observed_s,
        )

    if pane_before != pane_after:
        return Liveness(
            agent=agent,
            state=LIVE,
            detail=(
                f"{agent}: the pane CHANGED over {observed_s:.0f}s — something "
                f"is rendering, so this agent is alive. Do not touch it, "
                f"whatever banner is on screen"
            ),
            changed=True,
            observed_s=observed_s,
        )

    return Liveness(
        agent=agent,
        state=IDLE_CANDIDATE,
        detail=(
            f"{agent}: the pane did not change over {observed_s:.0f}s. This is "
            f"IDLE-OR-WEDGED and the two are indistinguishable from outside — a "
            f"healthy agent waiting at its prompt looks exactly like this. NOT "
            f"grounds to restart; it needs a direct ping with a >={MIN_OBSERVE_S:.0f}s "
            f"budget before anyone concludes anything"
        ),
        observed_s=observed_s,
    )
