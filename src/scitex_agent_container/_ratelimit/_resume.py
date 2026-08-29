"""The ONE mutation: wake an agent whose rate wall has lifted.

Separated from the pass so the pass stays a decision loop and this stays the
single irreversible act — the same split :mod:`.._reconcile._perform` makes,
for the same reason.

WHY A NUDGE AND NOT A RESTART
-----------------------------
The agent is ALIVE. Its tmux session, its Claude session and its whole
conversation survived the wall — the only thing that ended was the turn the
wall interrupted. Restarting it would destroy exactly the context that makes
resuming worth doing, and sac's auth matcher already reached this conclusion
from the other direction when it excluded 429 from the restart path: ``a
restart does not fix a rate wall``. Nor does it need fixing. It needs
continuing.

WHY ``_delivery.deliver`` AND NOT ``tmux send-keys``
---------------------------------------------------
Because a nudge that is not VERIFIED is the failure this fleet has already
had. :mod:`.._delivery` exists because "the operator saw a message land in a
peer's composer while the agent stayed idle" — text arrives in the compose
box and is never submitted, so the sender believes it delivered and the
agent never moved. That is indistinguishable, from the outside, from the
outage we are trying to end, and it is the exact state the 2026-08-28 agents
were found in: a prompt sitting unsent at ``❯`` for hours.

``deliver`` sends a tokenised payload, waits for idle, submits, and then
PROVES the payload left the compose box, returning a three-valued assessment
(``True`` / ``False`` / ``None`` for "could not tell"). We take only ``True``
as success. A resume we cannot prove is reported as a failure, never as a
win — a reviver that reports success it did not achieve leaves the operator
believing the fleet recovered.
"""

from __future__ import annotations

__all__ = ["RESUME_MESSAGE", "real_resume"]

#: What the woken agent is told. Deliberately short, deliberately about the
#: agent's OWN state rather than a new instruction: this pass knows that a
#: wall lifted, and knows nothing whatever about what the agent was doing.
#: Handing it a task would be this enforcer inventing work; pointing it back
#: at its own board is the only direction it is entitled to give.
RESUME_MESSAGE = (
    "Your provider rate limit has reset and this turn was resumed "
    "automatically by sac.resume-rate-limited-agents (you were paused, not "
    "restarted — your context is intact). Re-read your own scitex-cards board "
    "and continue the work that was interrupted; if nothing is outstanding, "
    "say so and stop."
)


def real_resume(name: str) -> bool:
    """Wake ONE local agent. ``True`` only on a PROVEN submission.

    ``_delivery.assess_delivery`` answers three-valued on purpose, and the
    two non-``True`` answers are both failures HERE even though they differ
    in kind: ``False`` is "the payload is still sitting unsent" and ``None``
    is "we could not tell". Neither is a resumed agent, and this enforcer's
    whole value is that an agent it could not recover becomes visible instead
    of being counted clean.
    """
    from .._delivery._assess import assess_delivery
    from .._delivery._deliver import deliver

    state = deliver(name, RESUME_MESSAGE)
    return assess_delivery(state).verdict is True
