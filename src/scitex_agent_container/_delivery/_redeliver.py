"""Self-healing redelivery — confirm, then retry, then switch route. Never silent.

:func:`._deliver.deliver` already answers \"what is known about this send\" as
a :class:`._state.DeliveryState`, and :func:`._assess.assess_delivery` folds
that into True / False / None. What was missing — the shape the operator
ruled out after the photo evidence (single-check Clock, ``connecting...``:
a sent-but-undelivered message sitting silently) — is the AUTOMATION that
acts on the fold:

1. **confirm** — assess every attempt; an unassessed send is an unconfirmed
   send, and unconfirmed is where the silence lived;
2. **retry** — a REFUTED send (complete information, generic failure) is
   worth resending on the same route, bounded;
3. **route-switch** — a NO_ROUTE send must never be retried on the same
   route (the remedy is distinct: go find out whether the agent is
   running); after same-route retries exhaust, try the other strategy
   once before escalating.

TWO SENDS THAT MUST NEVER BE RESENT
-----------------------------------
The fold's own contract binds this module, not just its tests:

* **UNKNOWN** — \"do not resend on this verdict — the message may well have
  landed, and a blind retry stacks a second copy into the peer's
  composer\" (:func:`._assess.assess_delivery`). One attempt, then
  escalate with the unresolved signals named.
* **UNSUBMITTED** — the payload ARRIVED and sits in the peer's composer;
  \"a caller must NOT resend (the text is already there; resending stacks
  a second copy into the same buffer)\" (``EXIT_UNSUBMITTED``). One
  attempt, then report the remedy: a single Enter into that pane.

So the retry budget below spends ONLY on generic REFUTED outcomes. Every
other non-delivered outcome returns after its first observation with its
remedy spelled out — that is the self-healing part that is actually safe.

STRATEGIES
----------
``"auto"`` / ``"sdk"`` / ``"tui"`` — the same vocabulary as
:func:`._route.resolve_route`. The switch fires only when the caller names
a ``switch_strategy`` different from the opening strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ._assess import (
    EXIT_DELIVERED,
    EXIT_NO_ROUTE,
    EXIT_REFUTED,
    EXIT_UNKNOWN,
    EXIT_UNSUBMITTED,
    assess_delivery,
)
from ._deliver import deliver
from ._state import DeliveryState

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "RedeliveryAttempt",
    "RedeliveryPolicy",
    "RedeliveryReport",
    "redeliver",
]

#: Same-strategy resends after the opening attempt, before any route-switch.
#: Small on purpose: each attempt pastes into a live peer, and a wedged
#: target does not unwedge on the ninth identical paste.
DEFAULT_MAX_RETRIES = 2

_ACTION_CONFIRMED = "confirmed"
_ACTION_RETRIED = "retried"
_ACTION_ESCALATED = "escalated"


def _switch_action(from_strategy: str, to_strategy: str) -> str:
    return f"route-switched {from_strategy}->{to_strategy}"


@dataclass(frozen=True)
class RedeliveryPolicy:
    """How far the healing automation may go before handing back to a human."""

    #: Same-route resends after the first attempt. Spent ONLY on generic
    #: REFUTED outcomes — never on UNKNOWN / UNsubmitted / NO_ROUTE.
    max_retries: int = DEFAULT_MAX_RETRIES

    #: Fallback strategy tried once after same-route retries exhaust (or
    #: immediately on NO_ROUTE). ``""`` disables switching. Must differ
    #: from the opening strategy to fire.
    switch_strategy: str = ""

    #: Pause between attempts. Zero by default — the underlying send
    #: already budgets its own waits; tests pass a no-op sleep.
    sleep_between_s: float = 0.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(
                f"max_retries must be >= 0 — got {self.max_retries}"
            )
        if self.sleep_between_s < 0:
            raise ValueError(
                f"sleep_between_s must be >= 0 — got {self.sleep_between_s}"
            )


@dataclass(frozen=True)
class RedeliveryAttempt:
    """One assessed send: what was tried, on which route, and the fold."""

    attempt: int
    strategy: str
    exit_code: int
    verdict: bool | None
    state: DeliveryState


@dataclass(frozen=True)
class RedeliveryReport:
    """Everything the automation did, and what the caller should do next."""

    agent: str
    delivered: bool
    attempts: tuple[RedeliveryAttempt, ...] = ()
    actions: tuple[str, ...] = ()
    remedy: str = ""

    @property
    def attempts_made(self) -> int:
        """How many real sends happened — the anti-silence number."""
        return len(self.attempts)

    def to_dict(self) -> dict:
        """JSON shape: the verdict, every attempt's fold, and the remedy."""
        return {
            "agent": self.agent,
            "delivered": self.delivered,
            "attempts_made": self.attempts_made,
            "actions": list(self.actions),
            "remedy": self.remedy,
            "attempts": [
                {
                    "attempt": a.attempt,
                    "strategy": a.strategy,
                    "exit_code": a.exit_code,
                    "verdict": a.verdict,
                    "state": a.state.to_dict(),
                }
                for a in self.attempts
            ],
        }

    def summary_text(self) -> str:
        """One mechanically-rendered paragraph for a drive-turn / log line.

        Rendered, never hand-written — the CCT injection port
        (:mod:`.._lifecycle._periodic_drive_port`) carries this text
        verbatim into the agent's inbox.
        """
        lines = [
            f"agent={self.agent} delivered={self.delivered} "
            f"attempts={self.attempts_made} actions={','.join(self.actions)}"
        ]
        for a in self.attempts:
            lines.append(
                f"  try{a.attempt} strategy={a.strategy} "
                f"exit={a.exit_code} verdict={a.verdict}"
            )
        lines.append(f"remedy: {self.remedy}")
        return "\n".join(lines)


def _assess_once(
    state: DeliveryState,
) -> tuple[bool | None, int, tuple[str, ...]]:
    """Fold one attempt into (verdict, exit_code, unresolved-names)."""
    assessment = assess_delivery(state)
    return assessment.verdict, assessment.exit_code(), assessment.unresolved


def redeliver(
    agent: str,
    message: str,
    *,
    strategy: str = "auto",
    policy: RedeliveryPolicy | None = None,
    deliver_fn: Callable[..., DeliveryState] = deliver,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RedeliveryReport:
    """Send ``message`` to ``agent`` with confirm → retry → route-switch.

    Every attempt is assessed; the assessment — not the send's return —
    picks the next step. Returns a :class:`RedeliveryReport` describing
    each attempt and the remedy when delivery did not confirm.

    ``deliver_fn`` is the seam: production passes :func:`._deliver.deliver`
    (the default); tests pass a plain function with the same
    ``(agent, message, *, strategy)`` signature carrying scripted states.
    """
    active = policy if policy is not None else RedeliveryPolicy()
    attempts: list[RedeliveryAttempt] = []
    actions: list[str] = []

    switch_to = active.switch_strategy.strip()
    switch_armed = bool(switch_to) and switch_to != strategy

    def _send(on_strategy: str) -> RedeliveryAttempt:
        state = deliver_fn(agent, message, strategy=on_strategy)
        verdict, exit_code, _ = _assess_once(state)
        return RedeliveryAttempt(
            attempt=len(attempts) + 1,
            strategy=on_strategy,
            exit_code=exit_code,
            verdict=verdict,
            state=state,
        )

    def _finish(delivered: bool, remedy: str) -> RedeliveryReport:
        return RedeliveryReport(
            agent=agent,
            delivered=delivered,
            attempts=tuple(attempts),
            actions=tuple(actions),
            remedy=remedy,
        )

    current = strategy
    opening = True
    retries_left = active.max_retries
    while True:
        record = _send(current)
        attempts.append(record)

        if record.verdict is True:
            actions.append(_ACTION_CONFIRMED)
            return _finish(
                True,
                f"delivered to {agent} on try{record.attempt} "
                f"(strategy={record.strategy}, exit={EXIT_DELIVERED})",
            )

        if record.exit_code == EXIT_NO_ROUTE:
            # Never resend where nothing was proven to exist.
            if switch_armed:
                actions.append(_switch_action(current, switch_to))
                current = switch_to
                switch_armed = False
                opening = False
                continue
            actions.append(_ACTION_ESCALATED)
            return _finish(
                False,
                f"no route to {agent} on strategy={record.strategy}: do NOT "
                f"resend — check whether the agent is running (tmux session "
                f"tui-{agent} / recorded session id), then retry",
            )

        if record.exit_code == EXIT_UNSUBMITTED:
            # The text is already in the peer's composer.
            actions.append("no-resend:payload-in-composer")
            return _finish(
                False,
                f"payload arrived at {agent} but was never submitted: send a "
                f"single Enter into the pane — do NOT resend the text",
            )

        if record.exit_code == EXIT_UNKNOWN:
            # A blind retry stacks a second copy into a possibly-live peer.
            actions.append("no-resend:unknown")
            unresolved = assess_delivery(record.state).unresolved
            return _finish(
                False,
                f"could not determine delivery to {agent} "
                f"(unread: {','.join(unresolved) or 'n/a'}): do NOT resend — "
                f"re-read the pane / route evidence first",
            )

        # Generic REFUTED with complete information: the only outcome a
        # same-route retry is safe for.
        if record.exit_code == EXIT_REFUTED and opening and retries_left > 0:
            actions.append(_ACTION_RETRIED)
            retries_left -= 1
            if active.sleep_between_s:
                sleep_fn(active.sleep_between_s)
            continue
        if record.exit_code == EXIT_REFUTED and switch_armed:
            actions.append(_switch_action(current, switch_to))
            current = switch_to
            switch_armed = False
            opening = False
            if active.sleep_between_s:
                sleep_fn(active.sleep_between_s)
            continue
        actions.append(_ACTION_ESCALATED)
        return _finish(
            False,
            f"delivery to {agent} refuted after {len(attempts)} attempt(s) "
            f"(last exit={record.exit_code}): escalate to the operator with "
            f"this report's attempt folds",
        )


# EOF
