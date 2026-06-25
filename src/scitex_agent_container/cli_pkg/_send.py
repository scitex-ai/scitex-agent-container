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
    keys: str | None = None,
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
        ValueError: When more than one of ``prompt`` / ``key`` / ``keys``
            is passed (or all are omitted). The MCP layer surfaces this
            as a tool validation error.
    """
    _key_modes = [v for v in (prompt, key, keys) if v]
    if len(_key_modes) > 1:
        raise ValueError("prompt, key and keys are mutually exclusive")
    if not _key_modes:
        raise ValueError("one of prompt, key or keys is required")

    from .._network.peer import PeerError
    from .._state.state_db import _resolve_host
    from ._send_resolve import resolve_send_endpoint

    current_host = _resolve_host(None)

    # Key passthrough is tmux-based (NOT /v1/turn), so it does not need a
    # bound a2a_port. Handle it before the A2A endpoint resolution. Cancel
    # keys (ESC / C-c / SIGINT) keep the local SIGINT semantics; every
    # other named key / sequence is delivered to the agent's tmux session.
    if key is not None or keys is not None:
        return _dispatch_keys(name, key=key, keys=keys, current_host=current_host)

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
            ),
        }
    if a2a_port is None:
        # A row exists but neither it nor the allocator carries a usable
        # port (sidecar-disabled spec, or a row written before the port
        # was resolved). Loud — there is no /v1/turn to reach.
        return {
            "status": "error",
            "error": (
                f"agent {name!r} has no a2a_port recorded "
                f"(no active instances-row port and no port_allocator "
                f"claim); cannot reach /v1/turn"
            ),
            "diagnosis": diagnose_send_failure(
                name,
                a2a_port=None,
                peer_host=peer_host or current_host,
                current_host=current_host,
            ),
        }
    if peer_host and peer_host != current_host:
        url = f"ssh://{peer_host}:{a2a_port}/v1/turn"
    else:
        url = f"http://127.0.0.1:{a2a_port}/v1/turn"

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


def _dispatch_keys(
    name: str,
    *,
    key: str | None,
    keys: str | None,
    current_host: str,
) -> dict[str, Any]:
    """Deliver ``key`` / ``keys`` to ``name`` and return a structured dict.

    Mirrors the CLI ``sac agents send --key/--keys`` passthrough for the
    MCP ``agent_send`` tool. Routing:

      * a single cancel key (ESC / C-c / SIGINT) → SIGINT the local
        runner pid (interrupt the turn), same as the CLI;
      * every other named key / sequence → validate against the tmux
        vocabulary and ``send-keys`` it into the agent's LOCAL tmux
        session.

    Key delivery is tmux-based, so it only works for an agent running on
    THIS host. A cross-host agent returns a loud error directing the
    caller to run the send on the peer (no silent mis-target).

    Returns:
        ``{"status": "ok", "route": "send-keys"|"interrupt", ...}`` on
        success; ``{"status": "error", "error": ...}`` on an unknown key,
        a missing tmux session, a dead/absent pid, or a cross-host agent.
    """
    import os
    import signal as _signal

    from .._runners._session_state import state_dir_for
    from .._runners._tmux._keys import (
        UnknownKeyError,
        parse_key_sequence,
        validate_keys,
    )
    from .._state.state_db import list_active_instances

    _cancel = {"ESC", "C-c", "SIGINT"}

    # Refuse to mis-target a cross-host agent — keys go to the local
    # tmux session only.
    rows = list_active_instances()
    matching = [
        r
        for r in rows
        if r.get("name") == name and str(r.get("host") or "") == current_host
    ]
    if not matching:
        return {
            "status": "error",
            "error": (
                f"agent {name!r} is not running locally on {current_host!r}; "
                "key passthrough is tmux-based and only reaches a local "
                "session. Run `sac agents send` on the host where the agent "
                "runs."
            ),
        }

    if key is not None and key in _cancel and keys is None:
        state_dir = state_dir_for(name)
        pid_file = state_dir / "pid"
        if not pid_file.is_file():
            return {
                "status": "error",
                "error": f"agent {name!r} has no pid file at {pid_file}",
            }
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, _signal.SIGINT)
        except (OSError, ValueError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "route": "interrupt", "pid": pid, "signal": "SIGINT"}

    tokens = parse_key_sequence(keys) if keys is not None else [key or ""]
    try:
        tmux_keys = validate_keys(tokens)
    except (UnknownKeyError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    from .._runners._tmux.multiplexer import get_multiplexer
    from ..config import load_config
    from ..config._resolve import resolve_config

    try:
        cfg = load_config(resolve_config(name))
    except Exception as exc:  # stx-allow: fallback (reason: unknown agent → structured error, not a traceback through MCP transport)
        return {"status": "error", "error": str(exc)}
    mux = get_multiplexer(cfg)
    if not mux.exists(cfg.screen_name):
        return {
            "status": "error",
            "error": (
                f"agent {name!r} has no live tmux session {cfg.screen_name!r}"
            ),
        }
    mux.send_keys(cfg.screen_name, *tmux_keys)
    return {
        "status": "ok",
        "route": "send-keys",
        "agent": name,
        "session": cfg.screen_name,
        "keys": tmux_keys,
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
    )

    # Fail loud on demonstrable unreachability (local probes only — a
    # cross-host port we cannot probe stays None and is NOT treated as
    # unreachable, which would be a false-positive failure).
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
        return {
            "status": "error",
            "error": (
                f"agent {name!r} sidecar is not listening on port {a2a_port}; "
                "it is not booted or the sidecar crashed — cannot dispatch"
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
