"""Project the auth-heal detector's output into :class:`.AgentState` rows.

WHAT THIS IS FOR
    PR #758 fixed the auth-heal detector by giving it a ``DetectionOutcome``
    (auth_failed / ok / unknown) and a ``Roster`` to check the reading against.
    Those are CORRECT, they are deployed, and this module does not replace them.
    They are, however, a SPECIFIC INSTANCE of the general shape: a
    ``DetectionOutcome`` is exactly a fleet of AgentStates projected onto one
    signal, ``is_login_required``, and a ``Roster`` is exactly the rule that the
    population is the roster rather than the enumeration.

    So this function states that relationship in code instead of in a comment,
    and the suite pins it: the projection must reproduce the detector's own
    partition, signal for signal. If the two ever drift apart, a test fails
    rather than two subsystems quietly disagreeing — which is the failure mode
    this whole card exists to end.

WHY THE AUTH-HEAL PASS WAS NOT REWRITTEN ONTO THIS
    It landed hours ago, it is deployed, and its gates were mutation-proved
    RED-then-green against the pre-fix source. Rewriting a just-verified
    remediation path to route through a new type would spend that verification
    to buy nothing the projection does not already give us. The bridge exists and
    is tested; the migration is a separate, checkable step.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ._state import AgentState

__all__ = ["states_from_detection"]


def states_from_detection(
    detection,
    *,
    roster: Sequence[str] = (),
    captures: Mapping[str, tuple[str | None, str | None]] | None = None,
    observer: str = "auth-heal",
    now: float | None = None,
) -> list[AgentState]:
    """Turn a ``DetectionOutcome`` (+ roster + raw panes) into AgentState rows.

    ``detection.auth_failed`` → ``is_login_required=True``
    ``detection.ok``          → ``is_login_required=False``
    ``detection.unknown``     → ``is_login_required=None``

    Any roster name the detection never classified at all gets a full row of
    Nones (:meth:`.AgentState.unknown`) rather than being dropped — the absence
    becomes a value. The raw panes, when supplied, travel with the rows so the
    journal can archive what was actually on screen, not just the verdict drawn
    from it.
    """
    captures = captures or {}
    seen: set[str] = set()
    states: list[AgentState] = []

    def _raw(name: str) -> dict[str, str]:
        pane1, pane2 = captures.get(name, (None, None))
        return {
            "pane_run1": pane1 if pane1 is not None else "",
            "pane_run2": pane2 if pane2 is not None else "",
        }

    for name in detection.auth_failed:
        seen.add(name)
        states.append(
            AgentState(agent=name, observed_at=now, observer=observer).with_signal(
                "is_login_required",
                True,
                "a system auth banner sat frozen directly above the prompt across "
                "both captures — corroborated wedge",
                **_raw(name),
            )
        )
    for name in detection.ok:
        seen.add(name)
        states.append(
            AgentState(agent=name, observed_at=now, observer=observer).with_signal(
                "is_login_required",
                False,
                "the pane was read and no frozen auth banner sat above the prompt",
                **_raw(name),
            )
        )
    for name in detection.unknown:
        seen.add(name)
        states.append(
            AgentState(agent=name, observed_at=now, observer=observer).with_signal(
                "is_login_required",
                None,
                "the pane could not be read, so nothing was learned about this "
                "agent's auth — which is not health",
                **_raw(name),
            )
        )

    for name in roster:
        if name not in seen:
            states.append(
                AgentState.unknown(
                    name,
                    "REGISTERED but absent from this reading entirely — the "
                    "enumeration is a reading of the fleet, not the fleet, and an "
                    "agent missing from it is unaccounted for, never healthy",
                    observed_at=now,
                    observer=observer,
                )
            )

    return sorted(states, key=lambda s: s.agent)


# EOF
