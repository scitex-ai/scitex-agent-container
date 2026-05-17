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

from typing import Any

from ._send_preflight import SshRunner, preflight_send_creds

__all__ = ["send_to_agent"]


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
    ssh_runner: SshRunner | None = None,
    lead_creds_path: Any = None,
) -> dict[str, Any]:
    """Send a turn to ``name``'s live A2A sidecar; return a structured dict.

    Returns:
        ``{"status": "ok", "response_text": str, "response_metadata":
        {...}}`` on success;
        ``{"status": "error", "error": "..."}`` when the agent isn't
        running, the row has no a2a_port, transport fails, or the
        sidecar returns non-200;
        ``{"status": "creds-expired", "error": "...", "agent": str}``
        when the lead's OAuth token (or the peer's, via ssh probe) is
        expired / near-expiry. Refuses to dispatch.
        ``{"status": "timeout", "error": "no response in <N>s"}`` when
        the sidecar doesn't reply within ``timeout_seconds``.

    Args:
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
    from .._state.state_db import _resolve_host, list_active_instances

    rows = list_active_instances()
    matching = [r for r in rows if r.get("name") == name]
    if not matching:
        return {"status": "error", "error": f"agent {name!r} not running"}
    row = matching[0]
    a2a_port = row.get("a2a_port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        return {
            "status": "error",
            "error": (
                f"agent {name!r} has no a2a_port recorded "
                f"(a2a_port={a2a_port!r}); cannot reach /v1/turn"
            ),
        }
    peer_host = str(row.get("host") or "")
    current_host = _resolve_host(None)
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

    try:
        reply, body = _post_turn(url, text, timeout_s=float(timeout_seconds))
    except PeerError as exc:
        msg = str(exc)
        # peer.py wraps timeouts as "peer timeout at <url> after Ns" and
        # ssh+curl timeouts as "ssh+curl timeout to ...". Sniff either
        # shape so the MCP tool can surface status="timeout" sharply
        # rather than burying the timeout inside a generic "error".
        if "timeout" in msg.lower():
            return {"status": "timeout", "error": f"no response in {timeout_seconds}s"}
        return {"status": "error", "error": msg}

    metadata: dict[str, Any] = {
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
