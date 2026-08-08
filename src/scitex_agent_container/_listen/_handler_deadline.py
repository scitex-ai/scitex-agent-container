"""Declared answer-by deadlines for the long-running listen lifecycle routes.

THE BUG THIS EXISTS TO KILL
---------------------------
``POST /agents`` had no answer-by guarantee. On the background path it spends
its time in four places (``_agent_exec.agents_start`` ->
``_credential_refresh_lock.run_brokered_launch``)::

    1. flock acquire (creds-refresh.lock)     BLOCKING, UNBOUNDED
    2. subprocess.run(sac agents start ...)   no timeout
    3. OAuth settle window                    up to 20s  <- HELD INSIDE THE LOCK
    4. post-ack liveness probe                up to 20s  <- outside the lock

A single spawn on an idle host is (2)+(4) ~= 22s, measured. But the settle
window (3) is held INSIDE the exclusive flock, so under N concurrent spawns
waiter N waits for N-1 predecessors x (launch + up to 20s) BEFORE its own
launch starts. Three concurrent spawns put the third caller past 60s.

Nothing in the protocol told that caller it was QUEUED. From outside, waiting
in a queue and talking to a dead host are the same silence — so the caller's
socket timeout became the error channel, and a spawn that SUCCEEDED was
reported to the fleet as a failure. That is the standing "I can't start
agents" complaint.

Raising the client timeout does not fix it. It was raised 20s -> 35s (PR #902),
which covers a single idle-host spawn and does not cover the queued case; no
single constant can, because the wait above is unbounded by construction.

THE CONTRACT
------------
The server declares a deadline and ALWAYS answers within it::

    accepted, completed, alive        200   true
    accepted, ran, observably failed  502   false
    accepted, STILL IN FLIGHT         202   unknown -- poll GET /agents/<name>/status
    bad body / ACL deny               400 / 403

``202`` is the load-bearing row and it is NOT an error: the spawn was accepted
and is running, we simply do not know the outcome yet. This is the SAME
three-valued discipline :mod:`._agent_exec_liveness` already applies one layer
down, in its own words: "an INCONCLUSIVE probe now returns 'no failure' rather
than manufacturing one: UNKNOWN authorises nothing." The probe stopped
manufacturing a failure from an inconclusive result; the HTTP layer in front of
it had not — it manufactured one by staying silent until the client gave up.

Because the deadline is declared HERE, the client timeout is DERIVED from it
(:func:`client_timeout_for`) instead of guessed independently. Two independent
constants that must satisfy ``client > server`` is a bug waiting for a load
spike; one constant plus a margin cannot drift.
"""

from __future__ import annotations

import time

__all__ = [
    "AGENT_START_DEADLINE_S",
    "CLIENT_MARGIN_S",
    "Deadline",
    "accepted_payload",
    "client_timeout_for",
]


# Answer-by guarantee for ``POST /agents``. The handler returns SOMETHING
# within this window — 200/502 when the outcome is known, 202 when it is not.
#
# 30s, and the floor is not arbitrary: a single spawn on an idle host costs the
# launch (~2s) plus the 20s post-ack liveness grace (_POST_ACK_LIVENESS_TIMEOUT_S,
# itself raised 5s -> 20s because a lost coin toss stamps ``startup_failed`` on a
# healthy agent). A deadline at or below ~22s would turn EVERY spawn into a 202
# and throw away the synchronous answer, which carries strictly more information
# than "poll me". 30s keeps the common case synchronous with ~8s of headroom;
# anything beyond it means the caller is genuinely queued behind another spawn,
# which is exactly the case that should be REPORTED rather than waited out.
AGENT_START_DEADLINE_S = 30.0

# How much longer a CLIENT waits than the server's own deadline. Covers the
# HTTP round trip, JSON encode/decode, and host scheduling jitter between the
# server deciding to answer and the client seeing bytes. Generous on purpose:
# the failure mode it prevents (client gives up while the server is composing
# its honest 202) is precisely the bug this module exists to kill, and the cost
# of being wrong in the safe direction is a few idle seconds.
CLIENT_MARGIN_S = 10.0


def client_timeout_for(server_deadline_s: float = AGENT_START_DEADLINE_S) -> float:
    """Return the socket timeout a client should use against a bounded route.

    Derived, never guessed: a client MUST outlive the server's own answer-by
    deadline, otherwise it destroys the very 202 that tells it the work is
    still in flight — and then reports the timeout as a failure, which is the
    original bug wearing a new number.
    """
    return float(server_deadline_s) + CLIENT_MARGIN_S


def accepted_payload(
    name: str,
    *,
    phase: str,
    deadline_s: float = AGENT_START_DEADLINE_S,
) -> dict:
    """Body for the ``202`` answer: accepted, in flight, outcome not yet known.

    ``phase`` says WHERE the deadline was reached — ``"launch"`` (still queued
    behind the credential boot gate, or the ``sac agents start`` subprocess is
    still running) or ``"liveness"`` (the launch returned rc=0 and the post-ack
    grace window had not concluded). The distinction matters to a caller
    deciding how long to keep polling: ``"liveness"`` means the agent has
    already been kicked off, ``"launch"`` means it may still be queued.

    ``poll`` is deliberately an EXISTING route. A 202 needs the outcome to be
    discoverable, and the agent's own liveness already is — inventing a job
    store to answer a question ``GET /agents/<name>/status`` already answers
    would add a second source of truth about whether an agent is running, which
    is how the two of them start disagreeing.
    """
    return {
        "name": name,
        "status": "accepted",
        "phase": phase,
        "deadline_s": float(deadline_s),
        "poll": f"/agents/{name}/status",
        "detail": (
            f"start of {name!r} was accepted and is still in flight at the "
            f"{float(deadline_s):.0f}s server deadline (phase={phase!r}). This is "
            f"NOT a failure: the spawn is running. Poll GET /agents/{name}/status "
            f"for the outcome — a failure will carry a startup_failed marker."
        ),
    }


class Deadline:
    """Monotonic answer-by budget for a single request.

    Uses ``time.monotonic`` so a wall-clock step (NTP, suspend/resume) can
    neither expire a healthy request early nor extend a hung one — the whole
    point is a bound that holds under exactly the disturbed conditions that
    produce the bug.
    """

    __slots__ = ("_expires_at",)

    def __init__(self, budget_s: float) -> None:
        self._expires_at = time.monotonic() + max(0.0, float(budget_s))

    def remaining(self) -> float:
        """Seconds left, floored at 0.0 (never negative — callers pass this
        straight to ``asyncio.wait_for`` / probe timeouts, which reject
        negatives)."""
        return max(0.0, self._expires_at - time.monotonic())

    def expired(self) -> bool:
        """True once the budget is exhausted."""
        return self.remaining() <= 0.0
