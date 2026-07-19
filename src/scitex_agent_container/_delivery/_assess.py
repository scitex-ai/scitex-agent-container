"""THE ONE PURE FOLD for a delivery. Every send verdict folds here or it is a bug.

Mirrors :func:`.._agentstate._assess.assess` exactly, because the failure it
prevents is the same one: N call sites each re-deriving "did that send work?" from
whatever subset of facts they happened to hold, and disagreeing.

THE AGGREGATE
-------------
``True``   every load-bearing signal is at its healthy value. The message
           arrived AND was submitted as a turn.
``False``  at least one load-bearing signal REFUTES **and no load-bearing signal
           is None** — a refutation with COMPLETE information.
``None``   at least one load-bearing signal is None, **and the output NAMES WHICH
           ONE.** An UNKNOWN that will not say what it could not read is the same
           shrug that let an operator report hours of messages as delivered.

Order is load-bearing: UNKNOWN outranks a False. A refutation drawn from partial
information is a guess, and guesses here get acted on — the remedy a caller
reaches for on a negative delivery verdict is TO SEND AGAIN, and re-sending into
an agent that did receive the first copy is how a peer gets the same instruction
twice and does the work twice.

THERE IS NO DECISIVE SHORT-CIRCUIT HERE
---------------------------------------
:mod:`._spec` grants decisiveness to nothing, so the branch that exists in the
sibling module is absent by construction rather than by omission — see that
module for why no delivery reading is first-hand enough to earn it. The
consequence is that this fold can never convict while blind, which is the correct
trade for an operation whose negative verdict triggers a retry.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._spec import DELIVERY_SIGNALS, DeliverySignalSpec
from ._state import DeliveryState

__all__ = [
    "EXIT_DELIVERED",
    "EXIT_NO_ROUTE",
    "EXIT_REFUTED",
    "EXIT_UNKNOWN",
    "EXIT_UNSUBMITTED",
    "DeliveryAssessment",
    "assess_delivery",
]

#: Exit codes. The verdict is THREE-VALUED and stays three-valued; the two extra
#: codes below are REFINEMENTS of ``EXIT_REFUTED``, never new verdicts, and each
#: names an outcome whose remedy is different from a generic failure.
#:
#: A WARNING THAT IS PART OF THE CONTRACT: an exit code is the least specific
#: thing this module produces, and small ints collide. ``click`` itself exits 2
#: on a USAGE ERROR — the same 2 this module spells COULD-NOT-DETERMINE — so a
#: caller that must tell those apart MUST read the JSON payload and MUST NOT
#: branch on the integer alone. That collision has already turned a missing verb
#: into an impersonated positive detection once. The code is for a cron that only
#: needs a coarse split; the payload is for anything that needs to be right.
EXIT_DELIVERED = 0
EXIT_REFUTED = 1
EXIT_UNKNOWN = 2

#: No route to the target at all — the session does not exist on a tmux server
#: that demonstrably CAN see other sessions. Distinct because the remedy is
#: distinct: do not retry the send, go find out whether the agent is running.
#: This is the mode that silently ate hours of coordination.
EXIT_NO_ROUTE = 3

#: The payload ARRIVED but was never submitted as a turn — it is sitting in the
#: peer's composer right now. Distinct because the remedy is a single Enter into
#: that pane, and because a caller must NOT resend (the text is already there;
#: resending stacks a second copy into the same buffer).
EXIT_UNSUBMITTED = 4


@dataclass(frozen=True)
class DeliveryAssessment:
    """The folded delivery verdict, plus which signals produced it and why."""

    agent: str

    #: True / False / None. The same ternary as every signal — the aggregate of
    #: tri-state values is itself tri-state, and collapsing it at the last inch
    #: would undo the whole exercise.
    verdict: bool | None

    #: The signals that FORCED this verdict: the refuting ones for False, the
    #: unresolved ones for None, all load-bearing ones for True.
    deciding: tuple[str, ...] = ()

    #: Every load-bearing signal that is None. Non-empty exactly when the verdict
    #: is None.
    unresolved: tuple[str, ...] = ()

    reason: str = ""

    def exit_code(self) -> int:
        """0 delivered · 1 refuted · 2 could-not-determine · 3 no-route · 4 unsubmitted.

        The refinements apply ONLY to a False verdict and only when the named
        signal is the one that refuted, so the mapping stays a function of the
        signals rather than of the caller's mood.
        """
        if self.verdict is True:
            return EXIT_DELIVERED
        if self.verdict is None:
            return EXIT_UNKNOWN
        if "is_route_resolved" in self.deciding:
            return EXIT_NO_ROUTE
        if "is_payload_submitted" in self.deciding:
            return EXIT_UNSUBMITTED
        return EXIT_REFUTED

    @property
    def is_unknown(self) -> bool:
        return self.verdict is None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "verdict": self.verdict,
            "exit_code": self.exit_code(),
            "deciding": list(self.deciding),
            "unresolved": list(self.unresolved),
            "reason": self.reason,
        }


def _refutes(state: DeliveryState, spec: DeliverySignalSpec) -> bool:
    """Is this signal OBSERVED at its unhealthy pole? ``None`` never refutes."""
    value = getattr(state, spec.name)
    return value is not None and value is not spec.healthy


def assess_delivery(state: DeliveryState) -> DeliveryAssessment:
    """Fold one :class:`.DeliveryState` into True / False / None. Pure.

    No IO, no clock, no environment: hand it a state and it returns the same
    answer forever, which is what makes the aggregation testable at all. Every
    branch below is reachable from a hand-built state, so the rule can be
    MUTATION-PROVED rather than trusted.
    """
    load_bearing = [s for s in DELIVERY_SIGNALS if s.load_bearing]
    unresolved = tuple(s.name for s in load_bearing if getattr(state, s.name) is None)

    # (1) UNKNOWN outranks a refutation. Name what could not be read — an UNKNOWN
    #     that will not say WHICH signal is unread is unactionable, and the
    #     unactionable warnings are the ones that get ignored into an outage.
    if unresolved:
        detail = "; ".join(
            f"{name}: {state.reason_for(name) or 'no reason recorded'}"
            for name in unresolved
        )
        return DeliveryAssessment(
            agent=state.agent,
            verdict=None,
            deciding=unresolved,
            unresolved=unresolved,
            reason=(
                f"COULD NOT DETERMINE — {len(unresolved)} load-bearing signal(s) "
                f"unread: {detail}. This is NOT a failed send and NOT a "
                f"successful one; it is the absence of a reading. Do not resend "
                f"on this verdict — the message may well have landed, and a "
                f"blind retry stacks a second copy into the peer's composer"
            ),
        )

    # (2) A refutation with COMPLETE information.
    refuting = tuple(s.name for s in load_bearing if _refutes(state, s))
    if refuting:
        detail = "; ".join(
            f"{name}: {state.reason_for(name) or 'observed at its unhealthy value'}"
            for name in refuting
        )
        return DeliveryAssessment(
            agent=state.agent,
            verdict=False,
            deciding=refuting,
            reason=(
                f"REFUTED with complete information — every load-bearing signal "
                f"was read, and {len(refuting)} of them refute: {detail}"
            ),
        )

    # (3) Everything load-bearing was read, and all of it is healthy.
    names = tuple(s.name for s in load_bearing)
    return DeliveryAssessment(
        agent=state.agent,
        verdict=True,
        deciding=names,
        reason=(
            f"DELIVERED and SUBMITTED — every load-bearing signal was OBSERVED "
            f"and is healthy ({', '.join(names)})"
        ),
    )


# EOF
