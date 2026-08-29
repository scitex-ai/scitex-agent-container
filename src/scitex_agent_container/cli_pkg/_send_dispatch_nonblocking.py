"""Non-blocking dispatch payload construction for ``send_to_agent``.

Extracted from :mod:`._send` (which hit the 512-line file budget) because
this is one cohesive responsibility: given a resolved endpoint, decide
whether the non-blocking (``wait=False``, default) dispatch can be
reported as reachable, and build that payload — or the loud
``status="error"`` when it demonstrably cannot.

The honesty fix (2026-08-29, the scitex-hpc incident)
------------------------------------------------------
The payload used to hardcode ``"delivered_subscriber_count": 1``
unconditionally once past the loud-failure gates below. Those gates fire
ONLY on an explicit ``False`` (a proven-dead pid, a locally-probed-refused
port); on the CROSS-HOST / in-container BROKERED path, ``pid_alive`` is
*always* ``None`` (never probed, by design — see
:mod:`._send_diagnosis_brokered`) and ``port_reachable`` is ``None``
whenever the target is not local. Neither gate can fire there, so EVERY
brokered non-blocking dispatch reported a fabricated ``1`` — including a
dispatch to an agent that is registered but has NEVER been started
(``scitex-hpc``: ``status=defined``, zero tmux sessions, a stale
``port_allocator`` claim). ``agent_send`` reported ``status="dispatched"``
+ ``delivered_subscriber_count: 1`` for it, three times, and nothing
surfaced the gap until the operator checked by hand.

The fix: ``delivered_subscriber_count`` is ``1`` ONLY when reachability
was ACTUALLY, LOCALLY verified (``diagnosis["port_reachable"] is True``).
Otherwise it is omitted — never a fabricated number — and the new
``status_code`` field (a ``scitex_dev.status.StatusCode``, ADR-0007)
carries the honest, machine-checkable verdict instead: ``http 202``,
``final=False``, with a message that says plainly whether reachability
was verified and names the probe to run next. This is also the answer to
"can the runtime tell registered-but-not-running from
running-and-accepted at this point" — in the brokered/cross-host case it
CANNOT, and ``status_code`` says so rather than inventing the stronger
claim the old hardcoded ``1`` implied.
"""

from __future__ import annotations

from typing import Any

from ._send_diagnosis import diagnose_send_failure
from ._send_status_code import dispatch_accepted_status_code
from ._send_track import build_track_command, build_track_command_argv, resolve_track_strategy

__all__ = ["dispatch_nonblocking", "unknown_lookup_payload"]


def unknown_lookup_payload(
    name: str,
    exc: Exception,
    *,
    current_host: str,
) -> dict[str, Any]:
    """Payload for "the host broker could not be asked" — UNKNOWN, not dead.

    The one thing this must never do is render an unperformed lookup as a
    stopped agent. ``registry_status`` comes back ``"unknown: …"``,
    ``pid_alive`` and ``boot_complete`` stay ``None``, and the message names
    the broker as the thing that failed — not the agent.

    Deliberately carries no ``status_code``: neither ``scitex`` code fits
    "we could not ask" (that is UNKNOWN, not a verdict this closed
    two-member vocabulary has a word for), and inventing one would be
    exactly the false confidence this module exists to remove.
    """
    from ._send_diagnosis_brokered import unknown_lookup_diagnosis

    return {
        "status": "error",
        "error": str(exc),
        "diagnosis": unknown_lookup_diagnosis(
            name, current_host=current_host, reason=str(exc)
        ),
    }


def dispatch_nonblocking(
    name: str,
    prompt: str,
    *,
    a2a_port: int,
    peer_host: str,
    current_host: str,
    url: str,
    metadata_extras: dict[str, Any],
    brokered: Any = None,
) -> dict[str, Any]:
    """Validate reachability, then return a non-blocking dispatch payload.

    Reachability is gathered via :func:`diagnose_send_failure`, which
    runs the SAME state probes (registry row, pid liveness, local sidecar
    TCP connect) the blocking path attaches on failure. We translate
    *demonstrable* unreachability into a LOUD ``status="error"`` — never a
    misleading "dispatched":

      * recorded pid is not alive       -> the process is dead
      * local sidecar port refuses TCP  -> the sidecar isn't listening

    A cross-host agent (``peer_host != current_host``) cannot be locally
    port-probed; we don't invent a verdict — the diagnosis records
    ``port_reachable=None`` and we proceed to ``dispatched`` (the
    backgrounded ``track_command`` is what ultimately surfaces a
    cross-host transport failure, loudly, when the caller runs it).

    On success the payload carries ``track_command`` — the backgroundable
    ``sac agents send`` CLI that delivers the prompt and streams the
    reply — so the caller fires-and-tracks instead of blocking inline.
    ``delivered_subscriber_count`` is ``1`` ONLY when a local probe
    actually confirmed the sidecar is listening; see the module docstring
    for why it is omitted otherwise rather than fabricated.
    """
    diagnosis = diagnose_send_failure(
        name,
        a2a_port=a2a_port,
        peer_host=peer_host,
        current_host=current_host,
        brokered=brokered,
    )

    # Fail loud on demonstrable unreachability (local probes only — a
    # cross-host port we cannot probe stays None and is NOT treated as
    # unreachable, which would be a false-positive failure).
    #
    # Both gates fire ONLY on an explicit ``False`` — never on ``None``.
    # That is the whole discipline: a probe we could not run leaves ``None``
    # and must not be read as a failed probe. On the brokered (in-container)
    # path ``pid_alive`` is deliberately always ``None`` — the host status
    # route exposes no pid, and importing a STALE one would make
    # ``os.kill(pid, 0)`` report a healthy, restarted agent as dead. See
    # :mod:`._send_diagnosis_brokered`.
    if diagnosis.get("pid_alive") is False:
        from ._send_status_code import agent_unavailable_status_code

        return {
            "status": "error",
            "error": (
                f"agent {name!r} recorded pid is not alive; the process "
                "crashed or was killed — cannot dispatch"
            ),
            "status_code": agent_unavailable_status_code(
                name, "the recorded pid is not alive"
            ).to_dict(),
            "diagnosis": diagnosis,
        }
    if diagnosis.get("port_reachable") is False:
        # An unbound /v1/turn port means THIS TRANSPORT cannot carry the turn.
        # It does NOT mean the agent is dead, and the old wording here ("it is
        # not booted or the sidecar crashed") asserted exactly that. Measured
        # on the live fleet 2026-07-14: only 5 of 47 registered agents had
        # /v1/turn bound at all — the other 41 held a port claim with nothing
        # listening, and several of them answered a2a messages that same
        # minute. Saying "crashed" here would hand the caller a death verdict
        # whose remedy (`--force --fresh`) destroys a healthy, working agent.
        #
        # No ``status_code`` here on purpose: this /v1/turn transport is
        # unreachable, but (per the measurement above) that is usually NOT
        # evidence the AGENT is unavailable — most of the fleet is reached
        # over the a2a bus instead, which this probe cannot see. Attaching
        # ``scitex AGENT_UNAVAILABLE`` here would repeat the exact
        # overclaim this module exists to remove, just pointed the other way.
        return {
            "status": "error",
            "error": (
                f"agent {name!r}: nothing is listening on a2a port {a2a_port}, "
                f"so the /v1/turn transport cannot deliver this turn. This is "
                f"NOT a death verdict — most agents in this fleet never bind "
                f"/v1/turn and are reached over the a2a subscriber channel "
                f"instead. Deliver with `sac a2a send {name} ...` (or the "
                f"a2a_send tool), which does not require this port. Do NOT "
                f"force-restart the agent on this signal"
            ),
            "diagnosis": diagnosis,
        }

    # WHICH VERB ACTUALLY REACHES THIS AGENT. Resolving the route here is what
    # stops the caller having to know whether the target runs TUI or SDK — the
    # detail that used to leak, and used to fail silently in the "delivered"
    # direction. Only the ROUTE is resolved (cheap); the paste/arrival/submit
    # half of delivery is deliberately not run, so this path stays non-blocking.
    # See :mod:`._send_track`.
    verified = diagnosis.get("port_reachable") is True
    strategy = resolve_track_strategy(name)
    track_command = build_track_command(name, prompt, strategy=strategy)
    payload: dict[str, Any] = {
        "status": "dispatched",
        "agent": name,
        "host": peer_host or current_host,
        "url": url,
        "a2a_port": a2a_port,
        # HONEST vs the pre-2026-08-29 shape: this is ``1`` only when a
        # local probe actually confirmed the sidecar is listening. Absent
        # (never a fabricated ``1``) when reachability is unverified — the
        # brokered/cross-host case. Read ``status_code`` for the full,
        # machine-checkable verdict either way.
        "delivered_subscriber_count": 1 if verified else None,
        "status_code": dispatch_accepted_status_code(
            name=name, verified=verified
        ).to_dict(),
        # Backgroundable CLI: run this in a background shell to deliver
        # the prompt + stream the reply without blocking this turn.
        "track_command": track_command,
        # Derived from the same builder as ``track_command`` above, so the two
        # renderings cannot disagree about the verb. They used to be two
        # independent literals.
        "track_command_argv": build_track_command_argv(
            name, prompt, strategy=strategy
        ),
        "note": (
            "non-blocking dispatch: the prompt was NOT yet delivered. Run "
            "`track_command` in a backgrounded shell to deliver it and "
            "stream the reply, or call agent_send(..., wait=True) to block "
            "inline."
        ),
    }
    payload.update(metadata_extras)
    return payload


# EOF
