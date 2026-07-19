"""THE ONE PURE FUNCTION. Every verdict in sac folds here or it is a bug.

「基準がたくさんあるじゃん？それらを全部 dataclass で持てばいいんだよ。ロジックが
おかしければそれでわかるでしょ？」 — hold every criterion, then fold them in ONE
visible place. When the logic is wrong you can see that it is wrong, because
there is one thing to look at instead of N call sites each quietly disagreeing.

THE AGGREGATE, exactly as specified
-----------------------------------
``True``   every load-bearing signal is at its healthy value. Trust it.
``False``  at least one load-bearing signal REFUTES **and no load-bearing signal
           is None** — a refutation with COMPLETE information.
``None``   at least one load-bearing signal is None, **and the output NAMES
           WHICH ONE.** An UNKNOWN that will not say what it could not read is
           the same shrug that let a wedged fleet look quiet.

Order is load-bearing: UNKNOWN outranks a non-decisive False. A refutation drawn
from partial information is a guess, and guesses here get acted on — the remedy a
caller reaches for on a negative verdict (restart, --force, kill) destroys the
thing it misdiagnosed.

THE ONE EXCEPTION — a DECISIVE signal
-------------------------------------
A signal the spec marks ``decisive`` short-circuits to False at its unhealthy
value even with Nones present. Without this, ANY single unreadable signal renders
UNKNOWN and blocks repair of a genuinely dead agent — and on a real fleet
something is always unreadable somewhere, so the pure rule alone would degrade
into a system that can observe problems and never fix them.

The safeguard is that decisiveness is not a property a call site may claim: the
spec grants it, and :func:`.._spec.validate_specs` refuses to grant it to
anything not DIRECTLY OBSERVED. ``is_process_alive=False`` read from the process
table means dead; the same claim read from a registry row is a declaration about
a pid and stays non-decisive. Only a corroborated, first-hand negative may
convict.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._spec import SIGNALS, SignalSpec
from ._state import AgentState

__all__ = ["Assessment", "assess"]

#: Exit codes. The exit code is the SUMMARY and nothing else — every raw signal
#: travels in the JSON («exit code はまとめた表現、信号はそのまま書く»). A caller
#: that needs to know WHICH signal decided reads the JSON; a cron only needs the
#: three-way split, and it must be a three-way split, because 0-or-1 is precisely
#: how "we observed nothing at all" got recorded as a healthy tick.
EXIT_TRUE = 0
EXIT_FALSE = 1
EXIT_UNKNOWN = 2


@dataclass(frozen=True)
class Assessment:
    """The folded verdict, plus which signals produced it and why."""

    agent: str

    #: True / False / None. The same ternary as every signal — the aggregate of
    #: tri-state values is itself tri-state, and collapsing it at the last inch
    #: would undo the whole exercise.
    verdict: bool | None

    #: The signals that FORCED this verdict: the refuting ones for False, the
    #: unresolved ones for None, all load-bearing ones for True.
    deciding: tuple[str, ...] = ()

    #: Every load-bearing signal that is None. Non-empty exactly when the verdict
    #: is None, EXCEPT under a decisive short-circuit — where it is the honest
    #: record of what we still did not know when we convicted anyway.
    unresolved: tuple[str, ...] = ()

    #: Set when a decisive signal short-circuited past unresolved signals.
    decided_by: str = ""

    reason: str = ""

    def exit_code(self) -> int:
        """0 = True · 1 = False · 2 = could-not-determine."""
        if self.verdict is True:
            return EXIT_TRUE
        if self.verdict is False:
            return EXIT_FALSE
        return EXIT_UNKNOWN

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
            "decided_by": self.decided_by,
            "reason": self.reason,
        }


def _refutes(state: AgentState, spec: SignalSpec) -> bool:
    """Is this signal OBSERVED at its unhealthy pole? ``None`` never refutes."""
    value = getattr(state, spec.name)
    return value is not None and value is not spec.healthy


def assess(state: AgentState) -> Assessment:
    """Fold one :class:`.AgentState` into True / False / None. Pure.

    No IO, no clock, no environment: hand it a state and it returns the same
    answer forever, which is what makes the aggregation testable at all. Every
    branch below is reachable from a hand-built state, so the rule can be
    MUTATION-PROVED rather than trusted.
    """
    load_bearing = [s for s in SIGNALS if s.load_bearing]
    unresolved = tuple(s.name for s in load_bearing if getattr(state, s.name) is None)

    # (1) DECISIVE. A directly-observed refutation from a signal the spec grants
    #     decisiveness convicts even with Nones outstanding — but it still
    #     REPORTS what remained unread, because "dead, and here is what we never
    #     managed to check" is a different claim from "dead, fully observed", and
    #     a reader must be able to tell them apart.
    for spec in load_bearing:
        if spec.decisive and _refutes(state, spec):
            why = state.reason_for(spec.name)
            return Assessment(
                agent=state.agent,
                verdict=False,
                deciding=(spec.name,),
                unresolved=unresolved,
                decided_by=spec.name,
                reason=(
                    f"{spec.name} is DECISIVE and was directly observed at its "
                    f"unhealthy value"
                    + (f" ({why})" if why else "")
                    + (
                        f"; this short-circuits past "
                        f"{len(unresolved)} unresolved signal(s) "
                        f"({', '.join(unresolved)}) because a first-hand "
                        f"observation of absence must not be blocked by a "
                        f"reading nobody could take"
                        if unresolved
                        else ", with every other load-bearing signal resolved"
                    )
                ),
            )

    # (2) UNKNOWN outranks a non-decisive refutation. Name what we could not read
    #     — an UNKNOWN that will not say WHICH signal is unread is unactionable,
    #     and unactionable warnings are the ones that get ignored into an outage.
    if unresolved:
        detail = "; ".join(
            f"{name}: {state.reason_for(name) or 'no reason recorded'}"
            for name in unresolved
        )
        return Assessment(
            agent=state.agent,
            verdict=None,
            deciding=unresolved,
            unresolved=unresolved,
            reason=(
                f"COULD NOT DETERMINE — {len(unresolved)} load-bearing signal(s) "
                f"unread: {detail}. This is not a failure and not a clean bill of "
                f"health; it is the absence of a reading, and it authorises "
                f"nothing destructive"
            ),
        )

    # (3) A refutation with COMPLETE information.
    refuting = tuple(s.name for s in load_bearing if _refutes(state, s))
    if refuting:
        detail = "; ".join(
            f"{name}: {state.reason_for(name) or 'observed at its unhealthy value'}"
            for name in refuting
        )
        return Assessment(
            agent=state.agent,
            verdict=False,
            deciding=refuting,
            reason=(
                f"REFUTED with complete information — every load-bearing signal "
                f"was read, and {len(refuting)} of them refute: {detail}"
            ),
        )

    # (4) Everything load-bearing was read, and all of it is healthy.
    names = tuple(s.name for s in load_bearing)
    return Assessment(
        agent=state.agent,
        verdict=True,
        deciding=names,
        reason=(
            f"every load-bearing signal was OBSERVED and is healthy "
            f"({', '.join(names)})"
        ),
    )


# EOF
