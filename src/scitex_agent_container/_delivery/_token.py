"""The arrival matcher — a SHORT INJECTED TOKEN, matched against a FLAT pane.

THE FAILURE THIS EXISTS TO END
------------------------------
On 2026-07-18 an operator confirmed a delivery by grepping the peer's pane for a
fragment of the prose he had sent. He got zero matches and concluded "not
delivered". The message HAD arrived — the Ink TUI had re-rendered and soft-wrapped
it, so the fragment he searched for no longer existed as a contiguous string
anywhere on screen. **The verification itself lied**, and it lied in the confident
direction: a negative from an instrument that cannot represent the positive.

Two rules follow, and both are implemented here rather than left to callers.

**Match a short INJECTED token, never prose.** The sender chooses the needle, so
the needle can be made short enough to survive a wrap and unique enough that a
match means something. Prose is the opposite on both counts.

**Flatten the haystack before matching.** Even a short token can be split by the
TUI's own box drawing — the composer renders inside a bordered box, so a wrapped
row arrives as ``…ab12│\\n│cd34…`` where the newline and the border are the TUI's
layout, not tmux's line wrapping. ``tmux capture-pane -J`` rejoins tmux's OWN
wraps and is necessary, but it cannot undo the TUI's, so :func:`flatten_pane`
strips everything that is not alphanumeric and lowercases the rest. A hex token
contains no stripped characters, so it survives any amount of border, whitespace
and wrapping the renderer inserts into it.

The collision risk this trades for is negligible and quantified: a 12-hex-char
token is one of 2**48 ≈ 2.8e14, and a flattened pane offers on the order of 1e4
starting positions, so a coincidental match runs about 1 in 1e10 per capture. A
false NEGATIVE from wrapping was a measured, recurring event; a false positive at
that rate is not.
"""

from __future__ import annotations

import re

from .._lifecycle.liveness_probe import generate_nonce, pane_has_nonce_echo

__all__ = [
    "DELIVERY_TOKEN_BYTES",
    "flatten_pane",
    "format_payload",
    "make_token",
    "pane_contains_token",
]

#: 6 bytes = 12 hex characters. Long enough that a flattened-pane collision is
#: ~1e-10 per capture, short enough to stay unobtrusive in a human-read message.
DELIVERY_TOKEN_BYTES = 6

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def make_token() -> str:
    """A fresh delivery token. Hex, via the fleet's existing nonce generator.

    Reuses :func:`.._lifecycle.liveness_probe.generate_nonce` rather than
    introducing a second source of randomness, so there is one answer to "where
    do sac's probe tokens come from". Hex-only also keeps the token visually
    unambiguous in a monospace TUI, where 0/O and 1/l/I collisions plague base64.
    """
    return generate_nonce(DELIVERY_TOKEN_BYTES)


def format_payload(message: str, token: str) -> str:
    """The wire form: a marked token, then the message.

    The token goes FIRST, on purpose. It is the part that must survive
    rendering, and the start of the payload is the position least likely to be
    split across a wrap boundary — the composer has a full row available at that
    point. (:func:`flatten_pane` makes the matcher wrap-proof anyway; this merely
    keeps the token legible to a HUMAN reading the pane, who has no flattener.)

    The ``sac-deliver:`` marker makes the token self-describing on the receiving
    end: an agent that sees it knows the message came through a verified send and
    that the sender is watching for arrival, rather than wondering what the
    stray hex is.
    """
    return f"[sac-deliver:{token}] {message}"


def flatten_pane(pane: str) -> str:
    """Strip every non-alphanumeric character and lowercase the rest.

    Defeats EVERY renderer artefact at once — soft wraps, box-drawing borders,
    ANSI-stripped whitespace runs, the lot — without needing to know which one
    the TUI applied. See the module docstring for why matching the raw capture
    is not good enough and has already produced a wrong answer in production.
    """
    return _NON_ALNUM.sub("", (pane or "").lower())


def pane_contains_token(pane: str | None, token: str) -> bool | None:
    """Did the token arrive on this pane? ``None`` when the pane is unreadable.

    THREE-VALUED on purpose: ``None`` is a capture that could not be taken, and
    it must never be spelled the same as a capture that was taken and came back
    clean. "Uncapturable" and "empty" reading alike is precisely how the wrong
    tmux server's emptiness got read as an agent's death.

    ``min_occurrences=1`` — NOT the default. :func:`pane_has_nonce_echo` defaults
    to ``2`` because it was written for a ROUND-TRIP ECHO probe ("repeat this
    nonce back to me"), where the sender's own prompt line contributes the first
    occurrence and only the agent's reply can contribute a second. Plain message
    DELIVERY has no round trip: the token is pasted once and appears once, so the
    echo module's default would report every successful delivery as a failure.
    The primitive is reused (one matcher, one place to fix) with the count set to
    what THIS question actually requires.
    """
    if pane is None:
        return None
    return pane_has_nonce_echo(flatten_pane(pane), token, min_occurrences=1)


# EOF
