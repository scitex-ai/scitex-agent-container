"""The PURE rule: may sac wake THIS agent from a rate wall, and is it time yet?

No IO, no clock of its own — ``now`` is passed in — so every leg below is
driven directly by tests instead of inferred from a live fleet.

THE THIRD SHAPE, and why it needed its own rule
-----------------------------------------------
sac already owns two agent-liveness shapes, and they are defined by each
other:

    no tmux session               a CORPSE            ``sac.fleet-reconcile``
    session + frozen auth banner  a WEDGE             ``sac.restart-login-expired-agents``

A rate wall is neither, which is exactly why nothing recovered from one. On
2026-08-28 a session limit stopped a set of agents at ~17:25 UTC and the
limit lifted at 19:10 UTC; nothing resumed until the operator asked at
20:56 UTC — one hour and forty-six minutes a human had to notice.

* ``fleet-reconcile`` did not act, and was RIGHT not to: the tmux sessions
  were still alive (measured on scitex-compute-04 — sessions created
  05:33 UTC were still listed at 21:15 UTC), so its rule returns
  ``OK``/``session-alive`` and hands off. It restarts corpses; there was no
  corpse.
* ``restart-login-expired-agents`` did not act, and was also right: its
  matcher deliberately excludes 429, saying so at the exclusion —
  ``a restart does not fix a rate wall``.

Both correct, and the agent stayed stopped. This rule is the missing third
answer.

THE PRINCIPLES THIS TABLE ENCODES
---------------------------------
1. **A wall is a PAUSE WITH A PUBLISHED END, not a fault.** While the wall
   is up the only correct action is NOTHING. :attr:`Verdict.WAITING` is a
   first-class success, not a deferred failure, and it is what makes hot-
   looping against a live limit structurally impossible: the resume branch
   is unreachable until ``now >= reset_at``. A reviver that retries into a
   standing limit burns the quota that ends the outage.
2. **Never guess when a wall lifts.** An unparseable reset clause is
   :attr:`Verdict.RESET_UNKNOWN` — hold, and REPORT. Guessing is how a
   reviver starts hammering.
3. **A moving pane is a working agent.** A banner at a different pane
   position across two captures means output is still being produced, so
   the agent is working, or quoting the incident in prose. Never touch it.
   Same freeze discipline as the auth healer, for the same reason: a false
   positive here interrupts an agent that was fine.
4. **"I could not look" is not "nothing is there"** — the rule sac's other
   two enforcers already live by, kept here so all three agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ._banner import LimitObservation

__all__ = ["Decision", "MANAGED_POLICIES", "Verdict", "decide"]

#: ``restart.policy`` values that put an agent under this enforcer's care —
#: the SAME set ``sac.fleet-reconcile`` uses, so a spec is either managed by
#: all of sac's liveness enforcers or by none of them. Two enforcers with
#: different ideas of who they are responsible for is a gap nobody can see.
MANAGED_POLICIES = ("always", "on-failure")


class Verdict(str, Enum):
    """What we concluded about ONE agent. Every leg is printed, never silent."""

    # --- reachable by the pure rule ---------------------------------------
    NOT_MANAGED = "NOT-MANAGED"  # restart.policy never/absent → not ours
    NO_SESSION = "NO-SESSION"  # a corpse → fleet-reconcile's, not ours
    UNREADABLE = "UNREADABLE"  # we could not read the pane. NOT "it is fine".
    NOT_LIMITED = "NOT-LIMITED"  # no rate wall on this pane
    MOVING = "MOVING"  # a wall is quoted but the pane advances → working
    WAITING = "WAITING"  # walled, and the wall has NOT lifted yet → hold
    RESET_UNKNOWN = "RESET-UNKNOWN"  # walled, reset unreadable → hold + report
    RESUME = "RESUME"  # walled, the wall HAS lifted → wake it

    # --- reachable only by the pass (what it did with a RESUME) -----------
    WOULD_RESUME = "WOULD-RESUME"  # --check: reported, not performed
    RESUMED = "RESUMED"
    FAILED = "FAILED"  # we nudged and it did not come back
    OVER_BUDGET = "OVER-BUDGET"  # out of hourly budget → reported, not nudged
    COOLING_DOWN = "COOLING-DOWN"  # inside the debounce → wait, do not report
    CAPPED = "CAPPED"  # this pass's cap spent; next pass retries
    BUDGET_UNKNOWN = "BUDGET-UNKNOWN"  # cannot read our OWN memory → refuse

    # --- the MODEL-CAP branch (:mod:`._switch_rule`, :mod:`._switch`) ------
    # A second remedy for a THIRD-AND-A-HALF shape: a wall that a model
    # SWITCH ends now, rather than one that only time ends. It shares this
    # vocabulary rather than growing a second one so the pass's counts, exit
    # code, event records and CLI rendering keep working unchanged — an
    # enforcer with two verdict alphabets is one whose reports cannot be
    # added up.
    SWITCH_MODEL = "SWITCH-MODEL"  # capped on a Fable model → switch it
    ALREADY_ON_TARGET = "ALREADY-ON-TARGET"  # already on the target → idempotent
    WOULD_SWITCH = "WOULD-SWITCH"  # --check: reported, not performed
    SWITCHED = "SWITCHED"  # switched, and the switch was VERIFIED
    SWITCH_FAILED = "SWITCH-FAILED"  # we switched and the cap is still up
    SWITCH_UNVERIFIED = "SWITCH-UNVERIFIED"  # we acted, we cannot prove it took


@dataclass(frozen=True)
class Decision:
    """A verdict plus WHY, in both machine and human form.

    ``reason`` is a short stable code (tests and JSON key off it); ``detail``
    is the sentence an operator reads. Both mandatory — a verdict whose cause
    is not stated is the silent skip this whole command exists to abolish.
    """

    verdict: Verdict
    reason: str
    detail: str


def decide(
    *,
    name: str,
    policy: str,
    session_present: bool | None,
    first: LimitObservation,
    second: LimitObservation,
    now: datetime,
) -> Decision:
    """Decide ONE agent's fate from facts alone. Pure — no IO, no clock.

    Parameters
    ----------
    policy
        ``spec.restart.policy``. Same opt-in as ``sac.fleet-reconcile``.
    session_present
        Whether a live tmux session for ``name`` was found. ``None`` means
        the enumeration itself failed, which is not the same as "no session".
    first, second
        Two readings of the SAME pane, taken an interval apart. The pair is
        what separates a parked agent from a working one: a banner that
        stayed at the same pane line across both is frozen; one that moved
        means the pane is still advancing.
    now
        The instant the SECOND capture was taken, timezone-aware. Compared
        against the reset the banner published — this comparison is the
        whole mechanism, so it is an argument and never a call to a clock.
    """
    if policy not in MANAGED_POLICIES:
        return Decision(
            Verdict.NOT_MANAGED,
            "policy-never",
            f"restart.policy={policy!r} — sac never promised to keep {name} "
            f"running, so there is nothing here to resume",
        )

    if session_present is None:
        return Decision(
            Verdict.UNREADABLE,
            "sessions-unreadable",
            f"could not enumerate tmux sessions, so we do not know whether "
            f"{name} has one. Refusing to infer anything from a reading we "
            f"failed to take",
        )
    if not session_present:
        return Decision(
            Verdict.NO_SESSION,
            "no-session",
            f"{name} has no live tmux session — a corpse is "
            f"`sac agents reconcile`'s to resurrect, not this pass's. A rate "
            f"wall is a pause in a LIVE session; there is no pane here to read",
        )

    if not (first.readable and second.readable):
        return Decision(
            Verdict.UNREADABLE,
            "pane-unreadable",
            f"{name}'s pane could not be captured on both reads "
            f"({second.detail or first.detail}) — no evidence, which is not "
            f"evidence that it is working",
        )

    if not second.limited:
        return Decision(
            Verdict.NOT_LIMITED,
            "no-wall",
            f"no rate wall on {name}'s pane — nothing for this pass to do",
        )

    if not first.limited or first.line_index != second.line_index:
        return Decision(
            Verdict.MOVING,
            "pane-advancing",
            f"{name}'s pane shows a rate-wall banner but it MOVED between the "
            f"two reads (line {first.line_index} → {second.line_index}), so "
            f"the pane is still producing output. That is an agent working, or "
            f"one quoting the incident — never one parked behind a wall",
        )

    if second.reset_at is None:
        return Decision(
            Verdict.RESET_UNKNOWN,
            "reset-unreadable",
            f"{name} is parked behind a frozen {second.window or 'rate'} wall "
            f"whose reset time could not be read ({second.detail}). Holding: "
            f"waking it on a guessed reset would hammer a wall that may still "
            f"be up, which costs the very quota that ends the outage. This is "
            f"reported, not swallowed",
        )

    if now < second.reset_at:
        remaining = second.reset_at - now
        return Decision(
            Verdict.WAITING,
            "wall-still-up",
            f"{name} is parked behind a {second.window or 'rate'} wall that "
            f"lifts at {second.reset_at.isoformat()} — {remaining} from now. "
            f"Holding, ON PURPOSE: this is the normal state during a limit, "
            f"not a fault, and touching it now would spend quota against a "
            f"wall that is still standing",
        )

    return Decision(
        Verdict.RESUME,
        "wall-lifted",
        f"{name} is parked behind a frozen {second.window or 'rate'} wall "
        f"whose published reset ({second.reset_at.isoformat()}) has PASSED — "
        f"the pause is over and nothing inside the agent will notice, because "
        f"the thing that would have noticed is the turn the wall stopped",
    )
