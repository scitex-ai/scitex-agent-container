"""Mode A action: per-agent transient rate-limit backoff (task #13 action layer).

Operator directive op-2026-06-12-13 (Telegram 12676 -> 12677) names two
sibling action-layer modules that consume
:func:`_account.rate_limit_classifier.classify_rate_limit_signal`'s
verdict: Mode A ``backoff_agent`` (this module) and Mode B
``rotate_account`` (:mod:`_account.rotate_account`). Mode A is the
TRANSIENT case — a server-side throttle (529) or a 429/403 on an
account that still has headroom. The account stays put; only the
retrying agent's OWN next attempt is delayed.

Why this needs to exist at all (not just reuse the generic supervisor
backoff already in ``_runners._session_conversation.run_conversation``):
that generic backoff is tuned for SDK/network blips and starts at
``restart_backoff_s`` (default 1.0s), doubling per attempt — 1s, 2s,
4s. A real Anthropic rate-limit window does not clear in 4 seconds;
retrying that fast just re-trips the same limit (the "retry storm"
the operator flagged). :func:`backoff_agent` enforces a floor
(:data:`DEFAULT_MIN_BACKOFF_S`) so a REACTIVE rate-limit hit always
waits long enough to have a real chance of clearing before the next
attempt, while still growing exponentially on repeated hits.

Consecutive-hit escalation
---------------------------
:mod:`_account.rate_limit_classifier`'s module docstring reserves one
decision for the action layer, not the pure classifier: "5 backoffs in
a row -> escalate to ROTATE — that lives on the backoff state machine,
not here." :func:`backoff_agent` owns that accounting:
``prior_consecutive_hits`` is the caller-maintained count of Mode-A
outcomes seen back-to-back (reset to 0 by the caller on any non-rate-
limit failure or a successful rotation); once the incremented count
reaches :data:`DEFAULT_ESCALATE_AFTER` the returned
:class:`BackoffDecision.escalate_to_rotate` flag is set. This covers a
low-usage account that keeps tripping 529/overload even though the
classifier's own usage-threshold check never fires (529 is ALWAYS
Mode A regardless of usage%, so without this escalation a persistently
overloaded account could retry forever without ever trying a
healthier one).

Pure function — no clock read (the caller supplies the "now" framing
implicitly via when it calls this), no sleep, no IO, no mutation. The
actual wait (``asyncio.sleep`` / ``asyncio.wait_for``) and the
consecutive-hit bookkeeping across calls belong to the caller
(:mod:`_runners._rate_limit_reactive`), mirroring how
:mod:`_account.rate_limit_classifier` stays pure and pushes clock/DB/
mutation to its callers.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_ESCALATE_AFTER",
    "DEFAULT_MIN_BACKOFF_S",
    "BackoffDecision",
    "backoff_agent",
]

# Floor for a REACTIVE rate-limit backoff delay, in seconds. Deliberately
# far above the generic supervisor's 1.0s starting point — see module
# docstring. 30s gives a real Anthropic 429/529 window a chance to clear
# without stalling a healthy agent for minutes on the first hit.
DEFAULT_MIN_BACKOFF_S = 30.0

# Base for the exponential ramp on repeated hits (doubles each time,
# same shape as the generic supervisor backoff, just with a higher
# floor applied via ``max()``).
DEFAULT_BASE_DELAY_S = 1.0

# Consecutive Mode-A hits (same signal classification, back-to-back,
# no intervening non-rate-limit failure or successful rotation) after
# which the caller should attempt Mode B (rotate) even though the
# classifier itself said BACKOFF. See "Consecutive-hit escalation"
# above.
DEFAULT_ESCALATE_AFTER = 5


@dataclass(frozen=True)
class BackoffDecision:
    """Mode A's decision for one reactive rate-limit hit.

    Attributes
    ----------
    delay_s
        Seconds the caller should wait before the next attempt on the
        SAME account. Always >= the floor passed to
        :func:`backoff_agent`.
    hit_count
        ``prior_consecutive_hits + 1`` — the new running count the
        caller should persist and pass back in as
        ``prior_consecutive_hits`` on the NEXT call (reset to 0 on any
        non-rate-limit outcome or a successful rotation).
    escalate_to_rotate
        ``True`` once ``hit_count`` reaches the escalation threshold —
        the caller should attempt Mode B (:func:`_account.rotate_account
        .rotate_account`) in addition to / instead of waiting
        ``delay_s`` on the same account.
    """

    delay_s: float
    hit_count: int
    escalate_to_rotate: bool


def backoff_agent(
    *,
    prior_consecutive_hits: int = 0,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    min_backoff_s: float = DEFAULT_MIN_BACKOFF_S,
    escalate_after: int = DEFAULT_ESCALATE_AFTER,
) -> BackoffDecision:
    """Return Mode A's decision: how long to wait, on the SAME account.

    Parameters
    ----------
    prior_consecutive_hits
        The caller-maintained count of Mode-A outcomes already seen
        back-to-back BEFORE this one (0 for the first hit). Must be
        >= 0.
    base_delay_s
        Starting point for the exponential ramp (before the floor is
        applied). Exposed for tests; production callers use the
        default.
    min_backoff_s
        The floor — see :data:`DEFAULT_MIN_BACKOFF_S`.
    escalate_after
        Hit-count threshold for :attr:`BackoffDecision.escalate_to_rotate`.

    Returns
    -------
    BackoffDecision

    Raises
    ------
    ValueError
        If ``prior_consecutive_hits`` is negative — a caller bug (the
        counter is caller-maintained and should never go negative);
        fail loud rather than silently treating it as 0.

    Pure: no sleep, no IO, no clock read.
    """
    if prior_consecutive_hits < 0:
        raise ValueError(
            f"prior_consecutive_hits must be >= 0, got {prior_consecutive_hits}"
        )
    hit_count = prior_consecutive_hits + 1
    exponential = base_delay_s * (2**prior_consecutive_hits)
    delay_s = max(min_backoff_s, exponential)
    return BackoffDecision(
        delay_s=delay_s,
        hit_count=hit_count,
        escalate_to_rotate=hit_count >= escalate_after,
    )
