"""Library-facing send helper shared by ``sac agents send`` + MCP ``agent_send``.

Single responsibility: POST one turn to a named agent's live A2A
sidecar (``/v1/turn``) and return a structured ``{"status", ...}``
dict. Reuses :func:`scitex_agent_container._network.peer.post_turn_to_url`
for the actual HTTP / ssh dispatch so there's exactly one place that
owns the transport (no duplication of urllib / ssh subprocess code).

Failure surfaces are sharp — no silent fallbacks:

  * Agent has no active state.db row     -> status="error", agent not running
  * Row has no a2a_port                  -> status="error", no a2a_port
  * Lead or peer creds expired           -> status="creds-expired" (loud)
  * Transport timeout                    -> status="timeout", informative msg
  * Sidecar returns non-200              -> status="error", HTTP code + body
  * Sidecar returns malformed JSON       -> status="error", malformed body
  * Cross-host (row.host != current)     -> ssh://<host>:<port> via peer.py

For unit testing without hitting the OS network stack, callers can
swap :data:`_post_turn` for a fake at the module level — the helper
resolves the symbol at call time, so the swap takes effect. The
preflight creds check accepts an explicit ``ssh_runner=`` callable so
the peer probe path is testable without an actual ssh subprocess.
"""

from __future__ import annotations

import shlex
from typing import Any

from .._listen._local_host import is_local_host
from ._send_diagnosis import diagnose_send_failure
from ._send_preflight import SshRunner, preflight_send_creds

__all__ = ["send_to_agent", "build_track_command"]


def build_track_command(name: str, prompt: str) -> str:
    """Return the backgroundable ``sac`` CLI that delivers + awaits a reply.

    The non-blocking dispatch path validates reachability and hands the
    caller this command instead of POSTing the turn itself. Running it in
    a backgrounded shell delivers the prompt to the agent's live session
    and streams the reply — so the lead's MCP turn never blocks on the
    agent's processing, yet the reply is still trackable.

    The ``sac agents send`` codepath is the SAME library helper this
    module backs (it routes prompt-style sends through /v1/turn for the
    running session), so the backgrounded command and the in-process
    ``wait=True`` path are behaviourally identical — there is no second,
    drifting delivery implementation.
    """
    return f"sac agents send {shlex.quote(name)} {shlex.quote(prompt)}"


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
) -> dict[str, Any]:
    """Dispatch a turn to ``name``'s live A2A sidecar; return a structured dict.

    Two modes, selected by ``wait``:

    * ``wait=False`` (DEFAULT, non-blocking) — validate that the agent is
      reachable (running, has a bound a2a_port, fresh creds, sidecar
      port accepting connections), then return PROMPTLY with
      ``status="dispatched"`` WITHOUT blocking on the agent's turn. The
      payload carries a ``track_command`` — a backgroundable ``sac
      agents send ...`` CLI the caller runs in a background shell to
      actually deliver the prompt and stream the reply. This is the path
      the MCP ``agent_send`` tool uses so the lead's turn never hangs on
      the agent's processing. Validation still fails LOUD: an agent that
      cannot possibly receive the turn (not running, no a2a_port, expired
      creds, sidecar port unreachable, recorded pid dead) returns the
      same ``error`` / ``creds-expired`` payload as the blocking path —
      never a misleading "dispatched".

    * ``wait=True`` (legacy synchronous) — POST the turn and BLOCK until
      the agent finishes the turn, then return its reply. Use only when
      the caller genuinely needs the response inline.

    Returns:
        ``{"status": "dispatched", "agent": str, "host": str, "url": str,
        "a2a_port": int, "track_command": str, "track_command_argv":
        [str, ...], "delivered_subscriber_count": 1}`` in the default
        non-blocking mode once reachability is validated;
        ``{"status": "ok", "response_text": str, "response_metadata":
        {...}}`` on a successful blocking (``wait=True``) reply;
        ``{"status": "error", "error": "..."}`` when the agent isn't
        running, the row has no a2a_port, the sidecar is unreachable,
        transport fails, or the sidecar returns non-200;
        ``{"status": "creds-expired", "error": "...", "agent": str}``
        when the lead's OAuth token (or the peer's, via ssh probe) is
        expired / near-expiry. Refuses to dispatch.
        ``{"status": "timeout", "error": "no response in <N>s"}`` when a
        blocking send's sidecar doesn't reply within ``timeout_seconds``.

    Args:
        wait: When ``True`` block on the agent's turn and return the
            reply inline (legacy behavior). When ``False`` (default)
            validate reachability and return a ``dispatched`` payload
            with a backgroundable ``track_command``.
        ssh_runner: Optional injection seam for the peer-side OAuth
            probe. Defaults to :func:`_send_preflight.default_ssh_runner`
            (real ssh). Tests pass a fake that returns a
            ``CompletedProcess`` with the desired ``returncode``.
        lead_creds_path: Optional override for the lead-local
            credentials path. Defaults to
            ``~/.claude/.credentials.json`` inside the preflight helper.
            Tests pass a ``tmp_path`` so the operator's real file is
            never read.

    Raises:
        ValueError: When ``prompt`` and ``key`` are both passed (or
            both omitted). The MCP layer surfaces this as a tool
            validation error.
    """
    if prompt and key:
        raise ValueError("prompt and key are mutually exclusive")
    if not prompt and not key:
        raise ValueError("either prompt or key is required")

    from .._network.peer import PeerError
    from .._state.state_db import _resolve_host
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
            return _unknown_lookup_payload(name, exc, current_host=current_host)
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
    # "" means LOCAL (dispatch over loopback); anything else is a genuinely
    # cross-host peer and goes over ssh below.
    #
    # This must be a LOOPBACK-AWARE test, not a string compare against
    # ``current_host``. The brokered path derives this host by parsing the
    # registry's ``turn_url`` (``_send_broker._host_from_turn_url``), and that
    # URL now correctly names ``127.0.0.1`` for a local agent — the address the
    # a2a sidecar actually binds — instead of the canonical hostname, which on
    # Debian/Ubuntu/WSL resolves to 127.0.1.1 and is refused. A bare
    # ``!= current_host`` would read that loopback literal as a REMOTE host and
    # try to ssh to ``ssh://127.0.0.1:<port>`` — turning a URL fix into a
    # comms outage. Any name that means THIS machine dispatches locally.
    peer_host = "" if is_local_host(endpoint.host) else endpoint.host

    if endpoint.source == "host_broker_unknown_agent":
        # The HOST — which can see the whole fleet — has no agent by this
        # name. This is the one definitive negative in the brokered path.
        return {
            "status": "error",
            "error": (
                f"agent {name!r} is not in the host fleet registry "
                f"(the host's `sac listen` returned 404 for it) — check the "
                f"name, or the agent was never registered on this host"
            ),
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
        return {
            "status": "error",
            "error": f"agent {name!r} not running",
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
            error = (
                f"agent {name!r} is registered on the host, but the host fleet "
                f"registry holds no a2a port claim for it (a claim is released "
                f"only at `sac agents stop` / --force); there is no /v1/turn "
                f"to reach"
            )
        else:
            # A row exists but neither it nor the allocator carries a usable
            # port (sidecar-disabled spec, or a row written before the port
            # was resolved).
            error = (
                f"agent {name!r} has no a2a_port recorded "
                f"(no active instances-row port and no port_allocator "
                f"claim); cannot reach /v1/turn"
            )
        return {
            "status": "error",
            "error": error,
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
        # Key-passthrough isn't wired into /v1/turn yet; the CLI handles
        # ESC via os.kill(SIGINT) on a local pid file. Surfacing this
        # as a loud error is the no-silent-fallback choice.
        return {
            "status": "error",
            "error": (
                f"key={key!r} dispatch not supported via send_to_agent; "
                "use the CLI's local SIGINT path (`sac agents send --key`)"
            ),
        }

    text = prompt or ""
    metadata_extras: dict[str, Any] = {}
    if model:
        metadata_extras["model"] = model
    if max_turns is not None:
        metadata_extras["max_turns"] = int(max_turns)

    # Preflight: refuse to dispatch on stale OAuth so a 401 doesn't
    # silently land in the in-container session.jsonl. Lead-local
    # creds are always probed; cross-host adds an ssh probe of the
    # peer's ~/.claude/.credentials.json.
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
        return _dispatch_nonblocking(
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
                "diagnosis": diagnosis,
            }
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
    }


def _unknown_lookup_payload(
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
    """
    from ._send_diagnosis_brokered import unknown_lookup_diagnosis

    return {
        "status": "error",
        "error": str(exc),
        "diagnosis": unknown_lookup_diagnosis(
            name, current_host=current_host, reason=str(exc)
        ),
    }


def _dispatch_nonblocking(
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
    ``delivered_subscriber_count`` is ``1`` (the validated live sidecar)
    so callers sharing the channel-send contract can branch uniformly.
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
        return {
            "status": "error",
            "error": (
                f"agent {name!r} recorded pid is not alive; the process "
                "crashed or was killed — cannot dispatch"
            ),
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

    track_command = build_track_command(name, prompt)
    payload: dict[str, Any] = {
        "status": "dispatched",
        "agent": name,
        "host": peer_host or current_host,
        "url": url,
        "a2a_port": a2a_port,
        # The validated live sidecar is the single subscriber for this
        # turn; mirrors the channel-send `delivered_subscriber_count`
        # contract so callers can branch uniformly.
        "delivered_subscriber_count": 1,
        # Backgroundable CLI: run this in a background shell to deliver
        # the prompt + stream the reply without blocking this turn.
        "track_command": track_command,
        "track_command_argv": ["sac", "agents", "send", name, prompt],
        "note": (
            "non-blocking dispatch: the prompt was NOT yet delivered. Run "
            "`track_command` in a backgrounded shell to deliver it and "
            "stream the reply, or call agent_send(..., wait=True) to block "
            "inline."
        ),
    }
    payload.update(metadata_extras)
    return payload
