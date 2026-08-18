"""The FAIL-LOUD contract for the send-side ``a2a_*`` MCP tools.

Extracted from :mod:`._channel_tools` (which hit the module size budget)
because it is one cohesive responsibility: deciding that a send
demonstrably failed, and rendering that failure so the CALLER CANNOT
MISTAKE IT FOR DELIVERY.

Why this module exists at all
-----------------------------
``a2a_send`` already detected the no-subscriber case and returned a body
carrying ``{"error": ...}``. But the MCP low-level ``Server.call_tool``
decorator wraps whatever a handler returns:

* a plain ``list[TextContent]`` becomes ``CallToolResult(isError=False)``
  — a **SUCCESS**, whatever the text inside happens to say;
* a ``CallToolResult`` is passed through **verbatim**, so a handler that
  wants to fail must say ``isError=True`` itself.

So the old shape was a false green: the tool reported "reached no live
subscriber" inside the body of a result flagged as successful. A caller
that trusted the MCP contract (rather than string-matching the payload)
read it as a delivered message. Agents silently swallowed each other's
messages and nothing in the control plane said so.

The three-state discipline
--------------------------
Delivery has THREE outcomes and they must never be collapsed into two:

* **delivered** — ``delivered_subscriber_count >= 1``. Success.
* **definitively NOT delivered** — an explicit ``0`` from the local
  publish path. The bus fanned out to nobody. This is a hard failure and
  the ONLY one this module raises for. It is EVIDENCE, not an absence of
  evidence.
* **could not determine** — the field is ABSENT (e.g. a cross-host
  forward whose reply does not carry it). Inventing a zero here would be
  a false-positive failure, so an absent count is NEVER read as zero and
  never fails. "I could not check" is rendered as neither success nor
  death.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.types import CallToolResult


class SendError(RuntimeError):
    """A send/push could NOT reach or wake the target agent.

    Raised by the send helper when delivery demonstrably failed:

    * the transport raised (agent down / connection refused),
    * the listen server returned a non-2xx status (delivery error), or
    * the publish reported ``delivered_subscriber_count == 0`` — no live
      inbox subscriber, so the message woke nobody.

    The send-side ``a2a_*`` tools render this via :func:`error_result`
    into an MCP result with ``isError=True`` — never a misleading
    success — and log it.

    ``code`` is the machine-readable failure class (one of the ``ERR_*``
    constants) and ``detail`` carries the structured fields a caller can
    branch on without re-parsing the human message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        target: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.target = target
        self.detail: dict[str, Any] = dict(detail or {})


# Machine-readable failure classes, echoed as ``code`` in the tool's
# error payload so a caller branches on the CLASS of failure instead of
# string-matching a human sentence.
ERR_NO_SUBSCRIBER = "no_live_subscriber"
ERR_UNREACHABLE = "unreachable"
ERR_DELIVERY_ERROR = "delivery_error"
ERR_LOOKUP_FAILED = "lookup_failed"
ERR_UNKNOWN_TARGET = "unknown_target"
ERR_TARGET_NOT_RUNNING = "target_not_running"

# What a 0-subscriber send hands the caller instead of a false success.
#
# It deliberately does NOT recommend ``agent_send`` / ``sac agents send``.
# That rail POSTs to the agent's ``/v1/turn`` sidecar port, which —
# measured on the live fleet, 2026-07-14 — only 5 of 47 registered agents
# actually bind. Its own failure path tells callers to use a2a instead
# (see ``cli_pkg._send``), so recommending it here would be a circular
# dead end for most of the fleet.
#
# It also does NOT recommend restarting the target. A 0-subscriber
# reading is a negative signal about ONE transport; it is not an
# observation of agent death. ``--force --fresh`` on it would destroy a
# healthy agent whose only problem is a detached inbox adapter.
NO_SUBSCRIBER_REMEDY: tuple[str, ...] = (
    "NOT LOST: sac listen persists every message to `channel_events` BEFORE "
    "publishing, and the target's inbox stream replays all undelivered rows "
    "on its next connect. Do NOT re-send this message — it would arrive "
    "twice once their adapter reconnects.",
    "To reach them NOW, use a rail that does not depend on their inbox "
    "adapter: a scitex-todo card assigned to them (durable, pull-based), or "
    "escalate to the operator.",
    "Do NOT force-restart the target on this signal. 0 subscribers means "
    "their inbox adapter is not attached; it is NOT evidence that the agent "
    "is dead, and a restart would destroy a healthy session.",
)


# What an UNREGISTERED target hands the caller. Deliberately the OPPOSITE
# advice to NO_SUBSCRIBER_REMEDY, because the two situations look identical
# from a 0-subscriber count and need opposite actions:
#
#     registered agent, adapter detached -> WAIT; it will replay on connect
#     unregistered name (a typo)         -> FIX THE NAME; nothing will ever
#                                           connect to drain the queue
#
# Measured 2026-08-09 by scitex-dev: they addressed this agent as "sac-04"
# (real name: scitex-agent-container-04) ALL DAY. Every send returned 200 with
# durably_queued=true, and NO_SUBSCRIBER_REMEDY told them, in those words, not
# to re-send — so they didn't. The messages went to a queue no adapter will
# ever attach to. They only found out by checking the registry for an
# unrelated reason.
#
# The scope limit that cost a second incident. The lookup behind this error
# reads ONLY this host's listen registry — each host keeps its own — so the
# old wording "no agent named X is registered … no adapter that will ever
# attach" asserted a FLEET-WIDE non-existence from a host-local instrument.
# Measured 2026-08-18: scitex-scholar (compute-03) hit exactly that message,
# concluded the assignee was dead, and re-homed its work — while the real
# agent sat live on compute-04, with pid, port and a healthy inbox measured
# from the other side minutes later. A miss here is UNKNOWN fleet-wide, not
# proof of absence: the message must state the population it observed, and
# the advice must forbid the ownership decision (declare dead, reassign
# work) the false death produced.
UNKNOWN_TARGET_REMEDY: tuple[str, ...] = (
    "FIX THE NAME AND RE-SEND — for a name that was never registered on THIS "
    "host. The lookup above was made against this host's listen registry, "
    "the only registry this host can see: a name never registered here has "
    "no local inbox stream to reconnect and no adapter here that could drain "
    "it, so waiting on this host will not deliver it. Call a2a_peers to list "
    "the names registered here; `agent_status` on a name that 404s is the "
    "same signal from the other side.",
    "DO NOT READ THIS AS A FLEET-WIDE VERDICT. Other hosts each keep their "
    "own registry, and none of them is visible from this one, so a name that "
    "lives on another host is indistinguishable here from a name that exists "
    "nowhere. This error is UNKNOWN fleet-wide, not proof the target is "
    "dead: do not conclude the agent is down, and do not reassign or re-home "
    "work on the strength of this signal — route the work durably (a "
    "scitex-cards card assigned to the target) or escalate to an agent that "
    "can see the other host's registry.",
)


# What a REGISTERED BUT STOPPED target hands the caller — the third cause of a
# 0-subscriber count, and the one whose advice was previously inverted.
#
# NO_SUBSCRIBER_REMEDY above says, correctly and in bold, "NOT LOST … do NOT
# re-send … it replays on their next connect". That is right for a live agent
# whose adapter is detached. For a STOPPED agent there is no next connect: no
# session exists to reconnect, so the row sits in `channel_events` until someone
# deliberately starts the agent. Handing the sender "wait, it is queued" is then
# the same failure as the `sac-04` typo incident (see UNKNOWN_TARGET_REMEDY) —
# a message parked forever behind reassuring advice.
#
# It still does NOT tell the caller to start the agent. Starting someone else's
# agent is an operator decision — measured 2026-08-12, 9 of 15 registered rows
# on one host were stopped, and a rail that nudges every blocked sender to start
# them would have restarted most of a fleet overnight, unasked and unobserved.
NOT_RUNNING_REMEDY: tuple[str, ...] = (
    "DO NOT WAIT FOR A REPLY. Unlike a detached inbox adapter, a stopped agent "
    "has no session that will reconnect, so the queued row will not drain on "
    "its own. This is the opposite of the no_live_subscriber case.",
    "The message IS durably queued (sac listen persisted it to channel_events "
    "before publishing), so it will be delivered IF this agent is started "
    "later. Do not re-send — that would deliver it twice.",
    "Do NOT start the target yourself to unblock your send. Whether a stopped "
    "agent should be running is an operator decision, not a side effect of "
    "someone wanting to message it.",
    "To make progress now: file a scitex-cards card assigned to the target "
    "(durable and pull-based, so it survives the agent being down), or route "
    "the work to a running peer, or escalate to the operator.",
)


def not_running_error(target: str) -> SendError:
    """Build the loud error for a send to a REGISTERED BUT STOPPED agent.

    Distinct from :func:`no_subscriber_error` because the count that produced
    both is identical — ``delivered_subscriber_count == 0`` — while the correct
    response is opposite. The distinguishing evidence is not the count: it is
    the ``fault`` the listen route now publishes next to it, derived from the
    host's tmux table (see ``_listen._inbox_fault``). Only a POSITIVELY observed
    absence reaches here; an unobservable session falls back to
    :func:`no_subscriber_error`, which is the safe reading.
    """
    return SendError(
        f"NOT DELIVERED — send to {target!r} reached no live subscriber, and "
        f"{target!r} IS NOT RUNNING: the listen daemon observed no live session "
        "for it, so its registry row has outlived its process. The message is "
        "durably queued, but NOTHING WILL DRAIN THAT QUEUE until the agent is "
        "started — there is no adapter to reconnect. Do not wait for a reply.",
        code=ERR_TARGET_NOT_RUNNING,
        target=target,
        detail={
            "delivered": False,
            "delivered_subscriber_count": 0,
            "durably_queued": True,
            "registered": True,
            "target_running": False,
            "what_to_do": list(NOT_RUNNING_REMEDY),
        },
    )


def suggest_names(target: str, known: list[str]) -> list[str]:
    """Registered names a caller plausibly MEANT by ``target``.

    Plain ``difflib`` is not enough here, and the motivating case proves
    it: ``difflib.get_close_matches("sac-04", [...])`` returns NOTHING for
    ``scitex-agent-container-04``. By character ratio they are unrelated
    strings — yet that is the exact mistake scitex-dev made all day, and a
    suggester that misses it would be decoration.

    Two fleet-specific signals carry the weight, because fleet names are
    not typos of each other, they are ABBREVIATIONS of each other:

    * ACRONYM — ``sac`` is the initials of ``scitex-agent-container``.
      This is the house naming convention, so it is the single most
      likely way a name goes wrong.
    * SHARED NUMERIC SUFFIX — ``…-04`` on both sides means the caller had
      the right instance and the wrong package name.

    Character similarity still contributes, so ordinary typos
    (``scitex-hubb``) are caught too.
    """
    import difflib
    import re

    def _tokens(text: str) -> list[str]:
        return [part for part in re.split(r"[^a-z0-9]+", text.lower()) if part]

    target_tokens = _tokens(target)
    if not target_tokens:
        return []

    scored: list[tuple[float, str]] = []
    for name in known:
        name_tokens = _tokens(name)
        if not name_tokens:
            continue
        score = difflib.SequenceMatcher(None, target.lower(), name.lower()).ratio()
        acronym = "".join(tok[0] for tok in name_tokens if tok[0].isalpha())
        head = target_tokens[0]
        if len(head) >= 2 and acronym.startswith(head):
            score += 0.5
        if target_tokens[-1].isdigit() and target_tokens[-1] == name_tokens[-1]:
            score += 0.3
        if score >= 0.5:
            scored.append((score, name))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:3]]


def unknown_target_error(target: str, known: list[str]) -> SendError:
    """Build the loud error for a send to a name NOT REGISTERED HERE.

    Separate from :func:`no_subscriber_error` because the two demand
    opposite responses. A detached adapter is a WAIT — the row is in
    ``channel_events`` and replays on the next connect. A name never
    registered on this host is a FIX — there is no local inbox stream to
    reconnect and no adapter on this host that could drain it, so the row
    is written to a queue nobody will ever read.

    The verdict is HOST-LOCAL, and the message says so. The lookup reads
    this host's listen registry only; every other host keeps a registry
    this host cannot see, and a name living on one of them produces the
    exact same miss. ``delivered=False`` and ``durably_queued=False`` are
    definitive FOR THIS HOST — an explicit 0 from the local publish.
    Fleet-wide, the answer to "does that agent exist?" is UNKNOWN, and
    ``detail["observation_scope"]`` pins that for machine readers so no
    caller has to parse prose to learn what population
    ``registered: false`` was measured against. Measured 2026-08-18:
    scitex-scholar read the old fleet-sounding wording as a death verdict
    and re-homed a live agent's work.

    ``known`` is the name list registered on THIS host's listen (as
    ``a2a_peers`` reports it); the closest matches are named in the
    message so a typo is a five-second correction instead of an
    indefinite wait.
    """
    suggestions = suggest_names(target, known)
    if suggestions:
        hint = " Did you mean: " + ", ".join(repr(s) for s in suggestions) + "?"
    elif known:
        hint = (
            f" {len(known)} agent(s) are registered on this host; "
            "call a2a_peers to list them."
        )
    else:
        hint = " No agents are registered on this host."
    return SendError(
        f"NOT DELIVERED FROM THIS HOST — no agent named {target!r} is "
        "registered on this host's listen, so this message was not sent "
        "here. NOTHING IS QUEUED HERE: a name never registered on this "
        "host has no local inbox stream to reconnect and no adapter here "
        "that could drain it, so waiting on this host will not deliver "
        "it. SCOPE OF THAT VERDICT: this host cannot see other hosts' "
        "registries, so a name that lives on another host is "
        "indistinguishable here from a name that exists nowhere — do not "
        "conclude the target is dead or reassign its work from this "
        "signal." + hint,
        code=ERR_UNKNOWN_TARGET,
        target=target,
        detail={
            "delivered": False,
            "delivered_subscriber_count": 0,
            # The load-bearing difference from no_subscriber_error. Claiming
            # True here is what made a real message wait forever.
            "durably_queued": False,
            "registered": False,
            # Every fact above was measured against THIS host's listen. Say
            # so where it matters most: fleet-wide, a miss is UNKNOWN, not
            # absence.
            "observation_scope": "host-local",
            "suggestions": suggestions,
            "what_to_do": list(UNKNOWN_TARGET_REMEDY),
        },
    )


def no_subscriber_error(target: str) -> SendError:
    """Build the loud error for an explicit ``delivered_subscriber_count == 0``.

    The message states exactly what was observed (the bus fanned out to
    zero subscribers) and — just as importantly — what was NOT observed
    (that the agent is dead, or that the message is gone).
    """
    return SendError(
        f"NOT DELIVERED — send to {target!r} reached no live subscriber "
        "(delivered_subscriber_count=0). The message woke nobody: "
        f"{target!r} has no inbox adapter attached to the channel bus "
        "(its session is not running `sac mcp channel`, or that adapter's "
        "SSE stream is not connected). Being listed as a running/`active` "
        "peer does NOT mean an agent is subscribed — registered is not "
        "reachable. Check `inbox_subscribers` on a2a_peers before "
        "handing work to a peer.",
        code=ERR_NO_SUBSCRIBER,
        target=target,
        detail={
            "delivered": False,
            "delivered_subscriber_count": 0,
            "durably_queued": True,
            "what_to_do": list(NO_SUBSCRIBER_REMEDY),
        },
    )


def unreachable_error(target: str, exc: Exception) -> SendError:
    """Build the loud error for a transport failure (connection refused …)."""
    return SendError(
        f"NOT DELIVERED — send to {target!r} failed: agent unreachable ({exc})",
        code=ERR_UNREACHABLE,
        target=target,
        detail={"delivered": False, "transport_error": str(exc)},
    )


def delivery_error(target: str, status: Any, body: Any) -> SendError:
    """Build the loud error for a server-side (5xx / bogus status) failure."""
    return SendError(
        f"NOT DELIVERED — send to {target!r} failed: listen returned "
        f"HTTP {status} ({body})",
        code=ERR_DELIVERY_ERROR,
        target=target,
        detail={"delivered": False, "http_status": status, "http_body": body},
    )


def _error_payload(exc: SendError) -> dict[str, Any]:
    """Project a :class:`SendError` onto the tool's JSON error body."""
    payload: dict[str, Any] = {
        "error": str(exc),
        "code": exc.code,
        "target": exc.target,
        "delivered": False,
    }
    payload.update(exc.detail)
    return payload


def error_result(exc: SendError) -> "CallToolResult":
    """Render a :class:`SendError` as an MCP result the caller CANNOT read
    as success.

    Returns a ``CallToolResult`` with ``isError=True``. The MCP low-level
    server passes a ``CallToolResult`` through verbatim (unlike a bare
    ``list[TextContent]``, which it stamps ``isError=False``), so this is
    the seam where "the message was not delivered" actually becomes a
    tool-level failure the calling model sees as one.

    The structured detail (target, failure ``code``, subscriber count,
    and what to do instead) is preserved in the payload — failing loudly
    must not cost the caller the information it needs to recover.
    """
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(_error_payload(exc), indent=2))
        ],
        isError=True,
    )


def lookup_error_result(message: str, *, target: str = "") -> "CallToolResult":
    """Render a non-send failure (unknown msg_id, unknown sender, unknown
    tool) as an ``isError=True`` result.

    These never delivered anything either, so they must not come back as
    a success body — same rule, different cause.
    """
    return error_result(
        SendError(message, code=ERR_LOOKUP_FAILED, target=target, detail={})
    )


__all__ = [
    "ERR_DELIVERY_ERROR",
    "ERR_LOOKUP_FAILED",
    "ERR_NO_SUBSCRIBER",
    "ERR_TARGET_NOT_RUNNING",
    "ERR_UNKNOWN_TARGET",
    "ERR_UNREACHABLE",
    "NOT_RUNNING_REMEDY",
    "NO_SUBSCRIBER_REMEDY",
    "UNKNOWN_TARGET_REMEDY",
    "SendError",
    "delivery_error",
    "error_result",
    "lookup_error_result",
    "no_subscriber_error",
    "not_running_error",
    "suggest_names",
    "unknown_target_error",
    "unreachable_error",
]
