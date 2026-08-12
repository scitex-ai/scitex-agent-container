"""A -> B is not a handshake. B -> A, OBSERVED BY A, is the only thing that counts.

The operator's item #7: before the lease moves, the target must contact the
source "agentically", and that contact must include a liveness/behaviour check —
not a port answering, not a process existing, but the agent's own loop taking a
turn and producing something only a working loop could produce.

WHY THE ONE-WAY VERSION IS WORTHLESS, MEASURED. On 2026-08-11 a2a between two
LIVE agents delivered nothing, and nobody noticed until a human asked. Every
one-way signal was green throughout: both processes ran, both sidecars listened,
both dispatch calls returned accepted. A relocation gated on "the target
started" would have handed over the lease into exactly that, and the failure
would then have looked like the 2026-08-07 one — started, healthy, doing nothing
— with the source already stopped and no way back (:func:`.._relocate_phases.
abort` refuses past HANDOVER, correctly).

So this module refuses to call anything a handshake unless FOUR things hold, and
each rules out a specific way the previous sentence's "green" was wrong:

    the challenge was ACCEPTED        the target's sidecar took the message
    a reply was OBSERVED BY THE SOURCE not "sent" — arrival, on the source side
    the reply CARRIES THIS NONCE      not a queued reply to an earlier prompt
    the reply PROVES WORK             an answer the loop had to compute

THE NONCE IS NOT CEREMONY. Without correlation, a reply that was already sitting
in the inbox from some earlier turn satisfies "a reply was observed", and a
relocation retried three times will eventually find one. The nonce is what makes
"a reply" mean "the reply to THIS challenge".

PROOF OF WORK, AND WHY IT IS NOT A PING. An echo proves the transport; it does
not prove the agent. The challenge must therefore ask for something the loop has
to DO — read a file, run a command, report a value it cannot know without
looking — and this module compares the answer it got with the answer the caller
computed independently. What that question is belongs to the caller, because it
depends on what the target can reach; what does NOT belong to the caller is the
option to skip it, so an absent expected answer is a refusal rather than a
waived requirement.

THREE-VALUED, and here the unknowns are the common case: a timeout waiting for a
reply is NOT "the target is broken", it is "I did not see one in the time I
waited". Those call for different actions — go and measure again with a longer
wait, versus go and fix the target — and folding the first into the second gets
a healthy relocation abandoned while folding it the other way is the 08-07 bug.

Pure: no transport, no clock, no sleeping. Observations in, a verdict out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CODE_NONCE_MISMATCH",
    "CODE_NOT_ACCEPTED",
    "CODE_NO_REPLY",
    "CODE_OK",
    "CODE_UNKNOWN",
    "CODE_WORK_NOT_PROVEN",
    "HandshakeFacts",
    "HandshakeVerdict",
    "evaluate_handshake",
]

#: Round trip complete: accepted, replied, correlated, and the work was proven.
CODE_OK: Final = 200
#: The target's sidecar did not take the challenge. Nothing was asked of the agent.
CODE_NOT_ACCEPTED: Final = 502
#: The challenge was accepted and no reply arrived. THE 2026-08-11 failure.
CODE_NO_REPLY: Final = 504
#: A reply arrived that is not the reply to this challenge.
CODE_NONCE_MISMATCH: Final = 409
#: The agent answered and the answer is wrong — it echoed, it did not work.
CODE_WORK_NOT_PROVEN: Final = 422
#: Something was not observed. Refuses as firmly as a failure, differently worded.
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class HandshakeFacts:
    """What was OBSERVED about one challenge/reply exchange.

    Every field is ``| None`` for NOT OBSERVED, which is deliberately distinct
    from an observed negative — the same discipline as
    :class:`.._relocate_preflight.TargetFacts`, and for the same reason: a
    dispatch call that raised must not be able to masquerade as "the target
    refused".

    ``observed_by`` names WHO saw the reply. It is required and it is not
    decoration: "the coordinator saw a reply" and "the source agent saw a reply"
    are different measurements, and a report that does not say which invites the
    reader to assume the stronger one.
    """

    #: The target's sidecar accepted the challenge for delivery.
    challenge_accepted: bool | None = None
    #: A reply arrived back — ARRIVAL, not dispatch.
    reply_observed: bool | None = None
    #: Who observed the reply, e.g. "the source agent" or "the coordinator".
    observed_by: str = ""
    #: The correlation token echoed in the reply, as read out of it.
    reply_nonce: str | None = None
    #: The answer the target gave to the proof-of-work question.
    reply_answer: str | None = None


@dataclass(frozen=True)
class HandshakeVerdict:
    """Whether the target proved it can do agent work, in ONE shape.

    ``proven`` is three-valued and there is no ``__bool__``: ``if verdict:``
    would be true for a refusal, and this is the last gate before the lease
    moves.
    """

    proven: bool | None
    code: int
    reason: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.proven not in (True, False, None):
            raise ValueError(
                f"HandshakeVerdict.proven must be True/False/None, got {self.proven!r}"
            )
        if not self.reason:
            raise ValueError(
                "HandshakeVerdict.reason must be non-empty — a refusal with no reason is not actionable"
            )
        if self.proven is True and self.code != CODE_OK:
            raise ValueError(
                f"HandshakeVerdict: proven=True must carry CODE_OK, got {self.code}"
            )
        if self.proven is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"HandshakeVerdict: proven=None must carry CODE_UNKNOWN, got {self.code}"
            )
        if self.proven is not True and not self.hint:
            raise ValueError(
                "HandshakeVerdict: a non-passing verdict must carry a hint saying what to do next"
            )


def _unknown(what: str, hint: str) -> HandshakeVerdict:
    return HandshakeVerdict(
        proven=None,
        code=CODE_UNKNOWN,
        reason=f"{what} was not observed",
        hint=hint,
    )


def evaluate_handshake(
    facts: HandshakeFacts,
    *,
    nonce: str,
    expected_answer: str,
) -> HandshakeVerdict:
    """Did the target prove, to the source, that it can do agent work?

    ``nonce`` is the correlation token the challenge carried; ``expected_answer``
    is what a working loop must have replied, computed by the caller from
    something the target had to go and look at.

    Both are required. An empty ``nonce`` would make every reply correlate and an
    empty ``expected_answer`` would make every reply prove the work, so each is
    refused at the top rather than silently weakening the gate — a check that can
    be disabled by passing nothing is a check that will eventually be disabled by
    passing nothing.
    """
    if not nonce:
        raise ValueError(
            "evaluate_handshake needs a non-empty nonce — without correlation, a reply "
            "left over from an earlier turn satisfies 'a reply was observed'"
        )
    if not expected_answer:
        raise ValueError(
            "evaluate_handshake needs a non-empty expected_answer — an echo proves the "
            "transport, not the agent, and this gate exists for the agent"
        )

    if facts.challenge_accepted is None:
        return _unknown(
            "whether the target accepted the challenge",
            "re-send the challenge and record whether the target's sidecar took it; "
            "a dispatch call that raised is not a refusal by the target",
        )
    if not facts.challenge_accepted:
        return HandshakeVerdict(
            proven=False,
            code=CODE_NOT_ACCEPTED,
            reason="the target did not accept the challenge, so its agent was never asked anything",
            hint=(
                "check that the target instance is running and its a2a sidecar is "
                "listening on the port the spec declares; nothing about the agent's "
                "behaviour has been measured yet"
            ),
        )

    if facts.reply_observed is None:
        return _unknown(
            "whether a reply came back",
            "watch the source side for the reply and say what you saw; a send that "
            "returned accepted says nothing about arrival",
        )
    who = facts.observed_by or "(nobody named)"
    if not facts.reply_observed:
        return HandshakeVerdict(
            proven=False,
            code=CODE_NO_REPLY,
            reason=(
                f"the challenge was accepted by the target and no reply reached {who}"
            ),
            hint=(
                "do NOT hand over the lease. Accepted-but-silent is the exact shape "
                "measured on 2026-08-11, where a2a between two live agents delivered "
                "nothing while every one-way signal stayed green. Check delivery end "
                "to end before retrying"
            ),
        )

    if facts.reply_nonce is None:
        return _unknown(
            "the correlation token in the reply",
            "read the nonce out of the reply body; without it, a reply queued from an "
            "earlier turn cannot be told from the answer to this challenge",
        )
    if facts.reply_nonce != nonce:
        return HandshakeVerdict(
            proven=False,
            code=CODE_NONCE_MISMATCH,
            reason=(
                f"the reply carries nonce {facts.reply_nonce!r}, not the {nonce!r} this "
                "challenge sent — it answers some other message"
            ),
            hint=(
                "drain the stale replies and re-run the handshake; a relocation retried "
                "until 'a reply' appears will eventually find one that proves nothing"
            ),
        )

    if facts.reply_answer is None:
        return _unknown(
            "the target's answer to the proof-of-work question",
            "read the answer out of the reply body; a correlated reply with no answer "
            "shows the message round-tripped, not that the loop did anything",
        )
    if facts.reply_answer != expected_answer:
        return HandshakeVerdict(
            proven=False,
            code=CODE_WORK_NOT_PROVEN,
            reason=(
                f"the target replied {facts.reply_answer!r} where a working loop had to "
                f"reply {expected_answer!r}"
            ),
            hint=(
                "the message path works and the agent's turn did not produce the right "
                "answer — check the target's credentials and its tool access before "
                "relocating onto it; this is the 'started, reported healthy, did "
                "nothing' shape, caught before the lease moved"
            ),
        )

    return HandshakeVerdict(
        proven=True,
        code=CODE_OK,
        reason=(
            f"round trip complete: {who} observed a reply carrying nonce {nonce} and "
            "the answer a working loop had to compute"
        ),
    )
