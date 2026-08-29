"""``StatusCode`` construction for the a2a send/dispatch boundary (ADR-0007).

Adopts ``scitex_dev.status.StatusCode`` — shipped in scitex-dev since
v0.48.0, well below sac's ``scitex-dev>=0.56.6`` floor — at exactly the
three boundaries ``scitex_dev.status``'s own ``spec/boundaries.yaml``
already declares for this package::

    a2a sidecar: send / reply              -> http, ack 202
    agent binary / verb resolution         -> scitex NOT_RESOLVABLE
    registry lookup of a non-running agent -> scitex AGENT_UNAVAILABLE

This module builds those three shapes for both send paths —
``cli_pkg._send.send_to_agent`` (the ``agent_send`` MCP tool) and the
``a2a_send`` MCP tool in ``_mcp._channel_tools`` — so the two surfaces
cannot drift on what "accepted" vs "unavailable" vs "unresolvable" means.

Nothing here invents a fourth scitex code. ``scitex`` is a CLOSED kind
with exactly two members (``NOT_RESOLVABLE``, ``AGENT_UNAVAILABLE``); see
``scitex_dev.status.spec.scitex-codes.yaml``. A third condition this
module cannot express with either code is left WITHOUT a ``status_code``
rather than forcing one — see ``agent_unavailable_status_code`` and the
callers that deliberately omit this field for a genuinely-unknown lookup
(the host broker could not be asked at all; that is UNKNOWN, not a verdict
this vocabulary has a word for).

Messages are observational (M1: no ``therefore`` / ``this means`` / ``the
fault is`` / ``root cause`` / ``must be caused by`` / ``proves that``) and
every non-final ``http 202`` names a runnable probe in backticks (M2).
"""

from __future__ import annotations

from scitex_dev.status import StatusCode

__all__ = [
    "agent_unavailable_status_code",
    "completed_status_code",
    "dispatch_accepted_status_code",
    "not_resolvable_status_code",
    "publish_accepted_status_code",
    "timed_out_status_code",
]


def dispatch_accepted_status_code(*, name: str, verified: bool) -> StatusCode:
    """``http 202`` for the non-blocking ``agent_send`` dispatch path.

    ``verified`` is ``True`` only when a LOCAL TCP probe actually connected
    to the target's ``/v1/turn`` port (see
    ``cli_pkg._send_diagnosis._port_reachable`` /
    ``cli_pkg._send_diagnosis_brokered.brokered_diagnosis``). It is
    ``False`` for the cross-host / in-container brokered case, where
    reachability is genuinely ``None`` (unmeasured) rather than proven —
    the exact gap the scitex-hpc incident exposed: this container cannot
    tell "registered but not running" from "running and accepting" at this
    point, so the honest report is "accepted, unconfirmed", never a
    fabricated delivery count.

    Both cases are ``202`` and both are ``final=False``: neither one has
    observed the turn being READ, only that dispatch was not demonstrably
    impossible (the loud ``AGENT_UNAVAILABLE`` gates — dead pid, refused
    port — already returned before this is ever called).
    """
    if verified:
        observed = (
            f"turn accepted for {name!r}; a local TCP probe confirmed its "
            "/v1/turn sidecar is listening"
        )
    else:
        observed = (
            f"turn accepted for {name!r}; reachability was NOT verified "
            "from here (cross-host or in-container lookup — no local probe "
            "ran), so this is dispatch, not a confirmed handshake"
        )
    return StatusCode(
        kind="http",
        code=202,
        message=(
            f"{observed}; not yet confirmed delivered — poll "
            f"`sac agents status {name}`, or run track_command and watch "
            "for a reply"
        ),
    )


def publish_accepted_status_code(name: str, delivered_subscriber_count: int) -> StatusCode:
    """``http 202`` for the ``a2a_send`` MCP tool's publish success path.

    Mirrors card ``sac-a2a-send-may-report-dispatch-as-arrival-20260821``'s
    own measured finding: a 200 asserts the event was PERSISTED to the
    durable rail before fan-out and ENQUEUED into N live subscriber
    queues — never that it was READ. ``delivered_subscriber_count`` is a
    REAL, measured number here (the publish call's own fan-out count), not
    a fabrication, so this differs from :func:`dispatch_accepted_status_code`
    only in having genuine evidence behind the count.
    """
    return StatusCode(
        kind="http",
        code=202,
        message=(
            f"persisted and enqueued to {delivered_subscriber_count} live "
            f"inbox subscriber(s) of {name!r}; not yet confirmed READ — "
            f"poll `a2a_inbox` on {name!r}, or wait for a reply"
        ),
    )


def agent_unavailable_status_code(name: str, reason: str) -> StatusCode:
    """``scitex AGENT_UNAVAILABLE`` — registered, but not running / not answering.

    Only for a POSITIVE finding of unavailability (an explicit ``False``
    from a liveness probe, or the host fleet registry reporting no live
    port claim / no live session) — never for an unmeasured gap, which
    stays ``202`` via :func:`dispatch_accepted_status_code`. This is the
    scitex-hpc case: a card routed to a defined-but-never-started agent
    must land here, not on a fabricated "delivered".
    """
    return StatusCode(
        kind="scitex",
        code="AGENT_UNAVAILABLE",
        message=(
            f"{name!r} is registered but not running: {reason}. Start it "
            f"with `sac agents start {name}`, or route the work to a "
            "running peer"
        ),
    )


def not_resolvable_status_code(name: str, reason: str) -> StatusCode:
    """``scitex NOT_RESOLVABLE`` — the name does not resolve to a registered agent.

    Per the admission rule in ``scitex-codes.yaml``: a negative result from
    a SINGLE resolver licenses ``NOT_RESOLVABLE``, never a stronger
    "does not exist anywhere" claim — this container's registry (or the
    one host it asked) is one resolver among possibly several.
    """
    return StatusCode(
        kind="scitex",
        code="NOT_RESOLVABLE",
        message=(
            f"{name!r} did not resolve: {reason}. Check the spelling with "
            "`sac a2a peers`"
        ),
    )


def completed_status_code(name: str) -> StatusCode:
    """``http 200`` — a blocking send (``wait=True``) got a real reply. Final."""
    return StatusCode(
        kind="http",
        code=200,
        message=f"{name!r} replied to the blocking turn; the exchange is complete",
    )


def timed_out_status_code(name: str, timeout_seconds: int) -> StatusCode:
    """``http 504`` — the CALLER stopped waiting. Says nothing about ``name``.

    Per ``status-codes.md`` §4: a client-side timeout is the honest
    ``504`` ("I stopped waiting"), never a peer-failure verdict — the
    2026-08-11 incident was a spawn client reporting a peer failure at a
    30s deadline while the peer worked the request for 5m12s.
    """
    return StatusCode(
        kind="http",
        code=504,
        message=(
            f"gave up waiting on {name!r} after {timeout_seconds}s; the "
            f"agent may still be working — poll `sac agents status {name}`"
        ),
    )


# EOF
