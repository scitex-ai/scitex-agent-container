"""Structured prompt/control dispatch shared by Python and MCP callers.

Container prompts and explicit host-listener requests submit through the
host's authenticated send route. A validated HTTP 202 is pending, not
completion, even for wait=True; poll the authored exchange without resending.
No local credential or transport fallback follows a host refusal.

Without that authority, send_to_agent resolves an endpoint and can attempt
policy-controlled autostart for a stopped local target. Prompt wait=False
returns a delivery plan, not a sent prompt. Remote reachability may remain
unknown; a local TCP probe also does not prove delivery. Prompt wait=True
attempts the turn and waits for a reply or reports a pending exchange/error.
Control keys use the resolved endpoint directly, independently of wait.

Transport belongs to _network.peer. Selected local Claude specs own their
credential preflight; foreign endpoints retain peer-owned provider authority.
A synthetic lead_creds_path applies only when no account/file is declared.
The accepted ssh_runner argument no longer probes guessed peer OAuth files.

Inspect receipts, diagnosis and status_code when present. Labels and transport
success do not establish task execution. Behavioral controls are in
cli_pkg/test__send_host.py, test__send_dispatch_nonblocking.py,
test__send_exchange_pending.py, test__send_preflight.py and test__send.py.
"""

from __future__ import annotations

from typing import Any

from ._send_diagnosis import diagnose_send_failure
from ._send_dispatch_nonblocking import dispatch_nonblocking, unknown_lookup_payload
from ._send_preflight import SshRunner, preflight_send_creds
from ._send_status_code import (
    agent_unavailable_status_code,
    completed_status_code,
    not_resolvable_status_code,
    timed_out_status_code,
)
from ._send_track import (  # noqa: F401  (re-export: long-standing import path)
    build_track_command,
    build_track_command_argv,
)

__all__ = ["send_to_agent", "build_track_command"]


def _pending_exchange_payload(name: str, pending: Any) -> dict[str, Any]:
    """Keep an accepted exchange pending instead of recasting it as failure."""
    from scitex_dev.status import StatusCode

    raw_body = pending.raw_body if isinstance(pending.raw_body, dict) else {}
    raw_receipt = raw_body.get("receipt")
    receipt = (
        dict(raw_receipt)
        if isinstance(raw_receipt, dict)
        else {"state": "pending", "final": False}
    )
    # The typed exception is only raised for a non-final accepted exchange.
    # Keep that canonical truth even if the most recent GET omitted the
    # optional human-facing receipt projection.
    receipt["state"] = "pending"
    receipt["final"] = False
    message = (
        f"turn accepted as {pending.exchange_id} and still pending; do not resend; "
        f"poll with `{pending.poll_hint}`"
    )
    return {
        "status": "pending",
        "agent": name,
        "exchange_id": pending.exchange_id,
        "receipt": receipt,
        "poll_hint": pending.poll_hint,
        "detail": str(pending),
        "status_code": StatusCode(kind="http", code=202, message=message).to_dict(),
    }


def _post_turn(url: str, text: str, *, timeout_s: float) -> tuple[str, dict[str, Any]]:
    """Reach the runner's /v1/turn and return ``(reply, body)``.

    Delegates to :func:`scitex_agent_container._network.peer.post_turn_to_url`
    so the urllib / ssh dispatch lives in exactly one module. The
    upstream helper returns only the ``reply`` string, so we wrap it in
    a tuple whose second element is the full body for callers that
    care about ``exit_after`` etc. (peer.py drops that metadata on the
    floor, so we synthesise an empty dict; the MCP tool degrades
    gracefully via ``response_metadata``).

    Raises :class:`scitex_agent_container._network.peer.PeerError` on
    transport failure or non-200 status (categorised by
    :func:`send_to_agent`).
    """
    from .._network.peer import post_turn_to_url

    reply = post_turn_to_url(url, text, timeout_s=timeout_s)
    return reply, {}


def send_to_agent(
    name: str,
    prompt: str | None = None,
    *,
    key: str | None = None,
    timeout_seconds: int = 120,
    model: str | None = None,
    max_turns: int | None = None,
    wait: bool = False,
    ssh_runner: SshRunner | None = None,
    lead_creds_path: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dispatch a prompt or control key and return a structured result.

    Supply exactly one truthy prompt or key. Container/declared-host prompts
    use the authenticated host route first. A validated 202 is pending even
    with wait=True; poll that exchange, do not resend, and do not infer
    admission or completion. Host refusal has no local fallback.

    Otherwise resolve the target endpoint, retaining policy-controlled local
    autostart and selected-spec credential checks. For prompts:

    * wait=False returns a dispatched delivery plan and track_command; no
      prompt has been delivered. Explicitly dead local probes refuse the
      plan, while unknown remote reachability remains unverified.
      delivered_subscriber_count is 1 after a successful local TCP probe,
      None otherwise; neither value proves prompt delivery.
    * wait=True attempts delivery and waits for the reply. Accepted unfinished
      exchanges return pending with exchange_id and poll_hint. A transport
      timeout is not evidence that the peer rejected or did not run the turn.

    Control keys post directly after endpoint resolution, independent of wait,
    and return control metadata rather than a prompt reply. Results can include
    ok, pending, dispatched, error, creds-expired or timeout. status_code and
    diagnosis are present only where the helper can supply them; consult those
    fields and receipts instead of promoting a label into completion evidence.

    Args:
        wait: Select delivery planning or inline prompt delivery. Does not
            override host pending receipts or change control-key behavior.
        ssh_runner: Legacy accepted argument; no peer OAuth probe is issued.
        lead_creds_path: Synthetic fallback for an unpinned local Claude spec;
            a declared account/file always owns its credential check.

    Raises:
        ValueError: Both prompt/key supplied, or neither is truthy.

    ``dry_run=True`` returns an argument-only plan before endpoint resolution,
    credential reads or lifecycle work. Prompt/control-key contents are hidden;
    no readiness, delivery or completion is established.
    """
    if prompt and key:
        raise ValueError("prompt and key are mutually exclusive")
    if not prompt and not key:
        raise ValueError("either prompt or key is required")

    if dry_run:
        from ._send_preview import build_send_preview

        return build_send_preview(
            name, prompt=prompt, key=key, model=model, max_turns=max_turns, wait=wait
        )

    from ._send_host import host_authority_declared, send_to_host

    if prompt and host_authority_declared():
        return send_to_host(
            name,
            prompt,
            model=model,
            max_turns=max_turns,
            timeout_s=float(timeout_seconds),
        )

    from .._network.peer import PeerError
    from .._state.state_store import _resolve_host
    from ._send_broker import (
        PeerLookupUnavailable,
        resolve_send_endpoint_via_host,
        should_broker_peer_lookup,
    )
    from ._send_resolve import resolve_send_endpoint

    current_host = _resolve_host(None)

    # --- Resolve WHERE the peer is ----------------------------------------
    # IN A CONTAINER the local state.db is a private, effectively empty
    # per-agent bridge DB that holds no row for ANY other agent, so the local
    # resolver below reports every peer as "not running" — measured
    # 2026-07-14: ``scitex-scholar`` came back ``stopped / pid=null /
    # a2a_port=null`` while the host's registry held ``pid=1777985
    # a2a_port=19037 ended_at=NULL`` for it and the agent had messaged us 90
    # seconds earlier. Broker the lookup to the host's ``sac listen`` — the
    # SAME door ``agent_status`` already goes through — so we read the real
    # fleet registry. See :mod:`._send_broker`.
    #
    # On a BARE HOST this is inert: ``should_broker_peer_lookup()`` is False
    # and the local resolver runs exactly as before.
    brokered = None
    if should_broker_peer_lookup():
        try:
            endpoint, brokered = resolve_send_endpoint_via_host(
                name, current_host=current_host
            )
        except PeerLookupUnavailable as exc:
            # We could not ASK the host. That is UNKNOWN — not dead. We refuse
            # to fall back to the blind local read, because its empty result
            # would masquerade as death, which is the bug we are fixing.
            return unknown_lookup_payload(name, exc, current_host=current_host)
    else:
        # Resolve the LIVE endpoint the same way ``a2a_send`` / the listen
        # forwarder do: active ``instances`` row port first, then the durable
        # ``port_allocator`` claim that survives a health-monitor restart.
        # Gating ONLY on the instances row (the old behaviour) caused the
        # "registry split-brain": a locally-running agent whose row went stale
        # (supervisor restart via ``runtime.start``, stale-lease clear) showed
        # ``a2a_port=null`` here even while ``a2a_send`` reached it on the bus.
        # See :mod:`._send_resolve` for the full root-cause writeup.
        endpoint = resolve_send_endpoint(name, current_host=current_host)

    a2a_port = endpoint.a2a_port
    peer_host = endpoint.host if endpoint.host != current_host else ""

    if endpoint.source == "host_broker_unknown_agent":
        # The HOST — which can see the whole fleet — has no agent by this
        # name. This is the one definitive negative in the brokered path.
        reason = "the host's `sac listen` returned 404 for it"
        return {
            "status": "error",
            "error": (
                f"agent {name!r} is not in the host fleet registry "
                f"({reason}) — check the name, or the agent was never "
                f"registered on this host"
            ),
            "status_code": not_resolvable_status_code(name, reason).to_dict(),
            "diagnosis": diagnose_send_failure(
                name,
                a2a_port=None,
                peer_host=current_host,
                current_host=current_host,
                brokered=brokered,
            ),
        }
    if endpoint.row is None and endpoint.source == "none":
        # No active instances row AND no durable allocator claim → the
        # agent is genuinely not running anywhere this host can see.
        # Start-on-send (operator order 2026-10-16): EVERY send kicks.
        # A message to a stopped agent starts it (doctrine-guarded:
        # managed restart.policy only, deliberate stops sacred, shared
        # restart budget) and then delivers, instead of dropping the
        # payload with "not running". See :mod:`._send_autostart`.
        from ._send_autostart import maybe_autostart_for_send

        started, autostart_note = maybe_autostart_for_send(name)
        if started:
            endpoint = resolve_send_endpoint(name, current_host=current_host)
            a2a_port = endpoint.a2a_port
            peer_host = endpoint.host if endpoint.host != current_host else ""
            if endpoint.row is not None or a2a_port is not None:
                pass  # re-resolved to something deliverable; fall through
            else:
                reason = (
                    "auto-started for send but still no live endpoint; "
                    f"autostart: {autostart_note}"
                )
                return {
                    "status": "error",
                    "error": f"agent {name!r} not running ({reason})",
                    "status_code": agent_unavailable_status_code(
                        name, reason
                    ).to_dict(),
                    "diagnosis": diagnose_send_failure(
                        name,
                        a2a_port=None,
                        peer_host=current_host,
                        current_host=current_host,
                        brokered=brokered,
                    ),
                }
        else:
            reason = (
                "no active instances row and no durable port-allocator claim; "
                f"autostart declined: {autostart_note}"
            )
            return {
                "status": "error",
                "error": f"agent {name!r} not running",
                "status_code": agent_unavailable_status_code(name, reason).to_dict(),
                "diagnosis": diagnose_send_failure(
                    name,
                    a2a_port=None,
                    peer_host=current_host,
                    current_host=current_host,
                    brokered=brokered,
                ),
            }
    if a2a_port is None:
        # No usable port. Loud — there is no /v1/turn to reach. The message
        # names the SOURCE of the verdict, so a reader never has to guess
        # whether it came from the real fleet registry or a blind local read.
        if endpoint.source == "host_broker_no_port":
            reason = (
                "the host fleet registry holds no a2a port claim for it "
                "(a claim is released only at `sac agents stop`)"
            )
            error = (
                f"agent {name!r} is registered on the host, but {reason}; "
                f"there is no /v1/turn to reach"
            )
        else:
            # A row exists but neither it nor the allocator carries a usable
            # port (sidecar-disabled spec, or a row written before the port
            # was resolved).
            reason = (
                "an active instances row exists but records no port and "
                "no port_allocator claim exists either"
            )
            error = (
                f"agent {name!r} has no a2a_port recorded "
                f"(no active instances-row port and no port_allocator "
                f"claim); cannot reach /v1/turn"
            )
        return {
            "status": "error",
            "error": error,
            "status_code": agent_unavailable_status_code(name, reason).to_dict(),
            "diagnosis": diagnose_send_failure(
                name,
                a2a_port=None,
                peer_host=peer_host or current_host,
                current_host=current_host,
                brokered=brokered,
            ),
        }
    if peer_host and peer_host != current_host:
        url = f"ssh://{peer_host}:{a2a_port}/v1/turn"
    else:
        url = f"http://127.0.0.1:{a2a_port}/v1/turn"

    if key:
        from .._network.peer import post_control_to_url

        try:
            response = post_control_to_url(url, key, timeout_s=timeout_seconds)
        except PeerError as exc:
            return {"status": "error", "error": str(exc), "agent": name}
        return {"status": "ok", "agent": name, "response_metadata": response}

    text = prompt or ""
    metadata_extras: dict[str, Any] = {}
    if model:
        metadata_extras["model"] = model
    if max_turns is not None:
        metadata_extras["max_turns"] = int(max_turns)

    # Only the local selected target can declare model credentials here.
    # Container sends have already reached the authenticated host authority.
    preflight_result = preflight_send_creds(
        name,
        peer_host=peer_host or current_host,
        current_host=current_host,
        lead_creds_path=lead_creds_path,
        ssh_runner=ssh_runner,
    )
    if preflight_result is not None:
        return preflight_result

    if not wait:
        # Non-blocking dispatch (default). Do NOT POST the blocking turn
        # — that would hang the caller until the agent finishes. Instead
        # validate that the agent can actually receive the turn and hand
        # back a backgroundable CLI that delivers + tracks the reply.
        return dispatch_nonblocking(
            name,
            prompt or "",
            a2a_port=a2a_port,
            peer_host=peer_host,
            current_host=current_host,
            url=url,
            metadata_extras=metadata_extras,
            brokered=brokered,
        )

    try:
        reply, body = _post_turn(url, text, timeout_s=float(timeout_seconds))
    except PeerError as exc:
        from .._network.peer import PeerTimeoutPending

        if isinstance(exc, PeerTimeoutPending) and exc.exchange_id:
            return _pending_exchange_payload(name, exc)
        msg = str(exc)
        # peer.py wraps timeouts as "peer timeout at <url> after Ns" and
        # ssh+curl timeouts as "ssh+curl timeout to ...". Sniff either
        # shape so the MCP tool can surface status="timeout" sharply
        # rather than burying the timeout inside a generic "error".
        #
        # Both surfaces carry a state-aware ``diagnosis`` gathered at the
        # moment of failure so the caller can tell "still booting" /
        # "alive & busy" / "dead" / "port unreachable" apart instead of
        # getting an opaque "no response".
        diagnosis = diagnose_send_failure(
            name,
            a2a_port=a2a_port,
            peer_host=peer_host,
            current_host=current_host,
            brokered=brokered,
        )
        if "timeout" in msg.lower():
            return {
                "status": "timeout",
                "error": f"no response in {timeout_seconds}s",
                "status_code": timed_out_status_code(name, timeout_seconds).to_dict(),
                "diagnosis": diagnosis,
            }
        # A non-timeout PeerError carries no code this module can honestly
        # attach as StatusCode without parsing peer.py's free-text message
        # (a parsed-then-wrong code is worse than none) — a deliberate
        # scope limit, not an oversight; see the PR body.
        return {"status": "error", "error": msg, "diagnosis": diagnosis}

    metadata: dict[
        str, Any
    ] = {  # stx-allow: STX-SAC001 (reason: this is the send-reply ``response_metadata`` contract — name+host+url+a2a_port of the turn target — NOT an A2A AgentCard; the v0-field heuristic false-positives on the name+url pair)
        "name": name,
        "host": peer_host or current_host,
        "url": url,
        "a2a_port": a2a_port,
    }
    metadata.update(metadata_extras)
    if "exit_after" in body:
        metadata["exit_after"] = body["exit_after"]
    return {
        "status": "ok",
        "response_text": reply,
        "response_metadata": metadata,
        "status_code": completed_status_code(name).to_dict(),
    }


# ``_unknown_lookup_payload`` and ``_dispatch_nonblocking`` moved to
# :mod:`._send_dispatch_nonblocking` (module size budget) — imported above
# as ``unknown_lookup_payload`` / ``dispatch_nonblocking``.
