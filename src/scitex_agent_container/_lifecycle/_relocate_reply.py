"""Reading an answer out of text that ALSO contains the question that asked for it.

The handshake's reply is looked for in places that necessarily hold the
challenge too — the agent's own transcript records the brief we sent it, and a
streamed session echoes the prompt. So a naive "does the nonce appear anywhere"
search matches OUR OWN MESSAGE and reports the target answered before it has
done anything at all. That is not a hypothetical: the brief literally contains
the line ``nonce=<nonce>``, because it has to — it is telling the agent what to
send back.

THE DISCRIMINATOR IS THE PLACEHOLDER, and it is deliberate rather than lucky.
:func:`._relocate_arrival.build_arrival_brief` writes the template as
``answer=<your answer to: …>`` — angle-bracketed, the universal "fill this in"
convention — and a real answer is a hostname, which never begins with ``<``. So
a candidate whose value opens with ``<`` is the QUESTION and is discarded. The
rule lives here, next to the parser that applies it, rather than as a comment on
the brief that a later edit could quietly invalidate: if the placeholder
convention ever changes, this module's tests fail, which is the point.

WHY BOTH HALVES MUST APPEAR TOGETHER. A nonce with no answer shows the message
round-tripped; an answer with no nonce could be replying to any earlier turn.
:func:`.._relocate_handshake.evaluate_handshake` wants them as separate
observations and refuses on either being absent, so this module reports each
independently rather than collapsing them into "found / not found".

NOTHING HERE DECIDES. It reads. Whether what it read proves the target can do
agent work is :mod:`_relocate_handshake`'s question, and keeping the two apart
is what lets this parser be tested against real captured transcript bytes with
no host, no agent and no verdict in sight.

Pure: text in, a reading out.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ANSWER_MARK",
    "NONCE_MARK",
    "ReplyReading",
    "read_reply",
]

NONCE_MARK = "nonce="
ANSWER_MARK = "answer="

#: What ends an answer value. A transcript is JSON-escaped, so the answer sits
#: inside a string literal and is terminated by the closing quote, by the
#: backslash of an escape (``\\n``), or by whitespace. Hostnames — the only
#: thing the proof-of-work question asks for — contain none of these.
_TERMINATORS = frozenset('"\\\n\r\t ,')


@dataclass(frozen=True)
class ReplyReading:
    """What was found in the searched text, as separate observations.

    ``nonce_seen`` and ``answer`` are reported apart because the handshake gate
    treats them as different facts: a correlated reply with no answer proves the
    round trip, not the work. ``candidates`` keeps every non-placeholder answer
    found, so a caller reporting a mismatch can say what it actually saw rather
    than only that it disagreed.
    """

    nonce_seen: bool
    answer: str | None
    candidates: tuple[str, ...] = ()
    #: Answers discarded for opening with ``<`` — i.e. the challenge's own
    #: template. Counted so "I found only the question I asked" is
    #: distinguishable in a log from "I found nothing at all"; the two look
    #: identical from the verdict and mean different things about delivery.
    placeholders_ignored: int = 0


def read_reply(text: str, *, nonce: str) -> ReplyReading:
    """Find ``nonce`` and the answer that accompanies it in ``text``.

    ``text`` may be anything the searched channel returned — a streamed reply, a
    slice of the agent's transcript, several concatenated lines. Only the
    regions that mention ``nonce`` are considered, so an answer belonging to an
    unrelated turn cannot be picked up.

    An empty ``nonce`` raises rather than matching everything: this function's
    whole job is correlation, and a call that supplies nothing to correlate
    against has lost the property it came here for.
    """
    if not nonce:
        raise ValueError(
            "read_reply needs a non-empty nonce — with nothing to correlate against, "
            "any answer in the text would satisfy the search, including the one in the "
            "challenge we sent"
        )
    if not text:
        return ReplyReading(nonce_seen=False, answer=None)

    # A transcript is one JSON object per line, so a whole reply lives on ONE
    # line together with the prompt that may have been echoed into the same
    # record. Splitting on lines therefore does not separate them, and the
    # placeholder rule below is what does.
    regions = [ln for ln in text.splitlines() if nonce in ln]
    if not regions:
        return ReplyReading(nonce_seen=False, answer=None)

    candidates: list[str] = []
    placeholders = 0
    for region in regions:
        for value in _answer_values(region):
            if value.startswith("<"):
                placeholders += 1
                continue
            if value:
                candidates.append(value)

    # The LAST distinct candidate wins: when a channel carries both an earlier
    # attempt and the current one, the later text is the more recent turn.
    answer = candidates[-1] if candidates else None
    return ReplyReading(
        nonce_seen=True,
        answer=answer,
        candidates=tuple(dict.fromkeys(candidates)),
        placeholders_ignored=placeholders,
    )


def _answer_values(region: str) -> list[str]:
    """Every ``answer=<value>`` in one region, values unterminated-trimmed."""
    out: list[str] = []
    start = 0
    while True:
        at = region.find(ANSWER_MARK, start)
        if at < 0:
            return out
        cursor = at + len(ANSWER_MARK)
        end = cursor
        while end < len(region) and region[end] not in _TERMINATORS:
            end += 1
        out.append(region[cursor:end])
        start = cursor if end == cursor else end
