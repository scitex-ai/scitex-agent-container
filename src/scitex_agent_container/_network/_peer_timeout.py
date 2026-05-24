"""Sender-side interpretation of the peer's honest 504 timeout body.

When agent A drives a turn into agent B via :func:`peer.post_turn` /
:func:`peer.post_turn_to_url` (and the ``sac peer post-turn`` CLI) and
B's turn exceeds the bounded HTTP wait, B's ``/v1/turn`` returns HTTP
504 with an *honest* body (PR #169,
``_runners/_session_http.py::_build_timeout_body``):

    {
      "status": "timeout_wait_elapsed",
      "detail": "<neutral one-liner>",
      "possibilities": [...],
      "timeout_s": <float>,
      "session_id": <str|None>,
      "heartbeat": {<phase/state, ts, elapsed_s, ...>} | null,
      "error": "<legacy '<N>s timeout' alias>"
    }

This module turns that body into a clear, neutral *interpretation* for
the calling agent — so the sender surfaces "the turn may still be
running" rather than the raw JSON.

A 504-still-running is NOT an exception-worthy failure, so the
interpretation is carried by :class:`PeerTimeoutPending` — a
:class:`PeerError` subclass that signals *in-progress*, not *failed*.
See its docstring for the return-vs-raise rationale.

The base :class:`PeerError` lives in :mod:`peer` (the canonical home of
the transport-error type); this module imports it. ``peer`` imports the
helpers here lazily inside its 504 branch, so there is no import cycle.
"""

from __future__ import annotations

from .peer import PeerError

__all__ = ["PeerTimeoutPending", "interpret_timeout_body", "TIMEOUT_STATUS"]

# Discriminator the runner stamps into the honest 504 body. Its presence
# is how the sender knows a 504 carries the structured "wait elapsed,
# turn may still be running" state rather than a genuine gateway failure.
TIMEOUT_STATUS = "timeout_wait_elapsed"


class PeerTimeoutPending(PeerError):
    """The bounded HTTP wait elapsed — the turn may STILL be running.

    Raised (instead of a plain :class:`PeerError`) when the peer returns
    HTTP 504 because its bounded ``/v1/turn`` wait elapsed. A 504 here is
    NOT a failure: the runner never cancels the SDK call, so the turn is
    usually still queued / draining and its result will land in the
    peer's session. This is therefore an *in-progress* signal, not an
    error.

    Why an exception subclass rather than a normal return value: the
    ``post_turn*`` contract is ``-> str`` where the string is the agent's
    *reply*. A timeout-pending is fundamentally not a reply, so returning
    it as the reply string would let an unaware caller mistake "still
    running" for the actual answer. Making it a ``PeerError`` *subclass*
    keeps existing ``except PeerError`` callers working unchanged, while
    callers that care can ``except PeerTimeoutPending`` first and treat
    it as in-progress (e.g. the CLI exits 0, not 2). See this PR's
    description for the return-vs-raise design fork.

    Attributes
    ----------
    interpretation : str
        Human-readable, neutral interpretation built for the calling
        agent (also ``str(exc)``).
    status : str | None
        The peer's ``status`` field (``"timeout_wait_elapsed"`` for an
        honest-shape body; ``None`` for an older peer that returned a 504
        without the honest body).
    timeout_s : float | None
        The peer's reported bounded-wait duration, when available.
    session_id : str | None
        The peer's session id, when the body carried one.
    heartbeat : dict | None
        The peer's verbatim heartbeat record, or ``None`` when the peer
        could not read live state.
    possibilities : list[str]
        The peer's neutral list of what the timeout might mean.
    raw_body : dict | None
        The full parsed JSON body, for callers that want everything.
    """

    def __init__(
        self,
        interpretation: str,
        *,
        status: str | None = None,
        timeout_s: float | None = None,
        session_id: str | None = None,
        heartbeat: dict | None = None,
        possibilities: list[str] | None = None,
        raw_body: dict | None = None,
    ) -> None:
        super().__init__(interpretation)
        self.interpretation = interpretation
        self.status = status
        self.timeout_s = timeout_s
        self.session_id = session_id
        self.heartbeat = heartbeat
        self.possibilities = possibilities or []
        self.raw_body = raw_body


def interpret_timeout_body(
    body: dict | None, *, fallback_label: str
) -> PeerTimeoutPending:
    """Build a :class:`PeerTimeoutPending` from a 504 response body.

    ``body`` is the parsed JSON of the peer's 504 response, or ``None``
    when the body was empty / unparseable. Robust to an OLDER peer that
    returns a 504 *without* the honest shape (no ``status`` /
    ``heartbeat`` / ``possibilities``): every field is read with
    ``.get`` and a sensible generic interpretation is produced instead
    of crashing on a missing key.

    ``fallback_label`` names the peer (URL or ``host:port``) for the
    generic message when the honest shape is absent.
    """
    body = body if isinstance(body, dict) else None

    timeout_s = body.get("timeout_s") if body else None
    session_id = body.get("session_id") if body else None
    heartbeat = body.get("heartbeat") if body else None
    if not isinstance(heartbeat, dict):
        heartbeat = None
    possibilities = body.get("possibilities") if body else None
    if not isinstance(possibilities, list):
        possibilities = []
    status = body.get("status") if body else None

    # Lead line: timeout is NOT necessarily a failure.
    if isinstance(timeout_s, (int, float)):
        lead = (
            f"Timeout after {float(timeout_s):.0f}s — this is NOT necessarily "
            "a failure. The turn may still be running on the peer."
        )
    else:
        lead = (
            "Timeout — this is NOT necessarily a failure. The turn may still "
            f"be running on {fallback_label}."
        )

    # Heartbeat line: report verbatim state, or say it's unavailable.
    hb_line = _format_heartbeat_line(heartbeat)

    # Possibilities line: join the peer's neutral list when present.
    if possibilities:
        poss_line = "Possibilities: " + "; ".join(str(p) for p in possibilities) + "."
    else:
        poss_line = (
            "Possibilities: turn still draining in the SDK; live state "
            "unavailable (older peer returned no structured timeout body)."
        )

    # Push-promise framing (PR #169 / dispatch-ledger direction).
    push_line = (
        "The result will land in the peer's session; an update will be "
        "pushed when there is news."
    )

    interpretation = "\n".join([lead, hb_line, poss_line, push_line])
    return PeerTimeoutPending(
        interpretation,
        status=status if isinstance(status, str) else None,
        timeout_s=float(timeout_s) if isinstance(timeout_s, (int, float)) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        heartbeat=heartbeat,
        possibilities=[str(p) for p in possibilities],
        raw_body=body,
    )


def _format_heartbeat_line(heartbeat: dict | None) -> str:
    """Render the one-line heartbeat summary for the interpretation.

    Reads the runner's heartbeat record (``state`` = phase, plus
    optional ``ts`` / ``elapsed_s`` activity fields) with ``.get`` so a
    differently-shaped or absent record degrades to a clear
    "unavailable" line rather than crashing.
    """
    if not isinstance(heartbeat, dict):
        return "Peer heartbeat: unavailable."
    phase = heartbeat.get("state")
    elapsed = heartbeat.get("elapsed_s")
    ts = heartbeat.get("ts") or heartbeat.get("heartbeat_at")
    parts: list[str] = []
    parts.append(f"phase {phase!r}" if phase is not None else "phase unknown")
    if ts is not None:
        parts.append(f"last beat {ts}")
    if isinstance(elapsed, (int, float)):
        parts.append(f"elapsed {float(elapsed):.0f}s")
    return "Peer heartbeat: " + ", ".join(parts) + "."
