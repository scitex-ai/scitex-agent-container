"""Outbound peer-to-peer client for the claude-session inbound endpoint.

Layer 3 of the external-orchestrator rollout. Layer 2 made it possible to
spawn + manage a remote agent that listens on ``POST /v1/turn``. This
module is the *outbound* side: an ergonomic helper for one runner (or
ops script) to drop a new turn onto another agent's persistent SDK
conversation.

Two surfaces:

* ``post_turn_to_url(url, text, *, exit_after=False, timeout_s=600.0)``
  — low-level. Posts the JSON envelope to a known URL. Returns the response
  ``text`` for synchronous runners. For an asynchronous HTTP 202 receipt it
  either polls the named exchange or returns the validated non-final receipt,
  according to ``wait_for_final``.

* ``post_turn(agent_name, text, *, exit_after=False, timeout_s=600.0)``
  — high-level. Resolves the target agent's YAML via the project +
  home + env discovery chain, picks the right host:port, and POSTs.

URL resolution rules for ``post_turn(agent_name, ...)``:

* Local agent (``spec.host`` empty / matches the calling host) →
  ``http://127.0.0.1:<port>/v1/turn``.
* Remote agent (``spec.host`` pinned to a different host) →
  ``http://<spec.host>:<port>/v1/turn``. The agent YAML's
  ``spec.a2a.host`` MUST be ``0.0.0.0`` (or a LAN-visible address)
  for this to work — loopback-only listens aren't reachable from
  the caller's host. We raise a clear error in that case so the
  user fixes the YAML rather than getting an opaque connection
  refused.

Auth on the wire (WI-2 / WI-4, 2026-05-21): cross-host calls into
another sac listen's ``message:send`` carry the destination host's
listen bearer, pulled from
``~/.scitex/agent-container/peer-tokens/<peer-host>.token`` on the
caller's side (registered via ``sac host add-peer <host> <token>``).
The destination's :class:`BearerAuthMiddleware` admits the request
as an *administrative* caller; the destination's ACL then gates on
``metadata.from_agent`` per handoff §4 ("ACL is enforced at the
receiving host").
"""

from __future__ import annotations

import json
import scitex_logging as slogging
import time
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "post_turn",
    "post_turn_to_url",
    "post_control_to_url",
    "resolve_peer_url",
    "PeerError",
    "PeerTimeoutPending",  # noqa: F822 - lazy export resolved by __getattr__
]


def post_control_to_url(
    url: str, key: str, *, timeout_s: float = 10.0
) -> dict[str, Any]:
    """POST one explicit UI key to a live TUI's neutral control endpoint."""
    if not url.endswith("/v1/turn"):
        raise PeerError(f"control base URL must end in /v1/turn (got {url!r})")
    if key not in {"Enter", "Escape", "ESC", "C-c", "SIGINT"}:
        raise PeerError(
            f"unsupported UI control key {key!r}; use Enter, Escape, or C-c"
        )
    control_url = url.removesuffix("/v1/turn") + "/v1/control"
    body = json.dumps({"kind": "control", "action": "ui.key", "key": key}).encode()
    if control_url.startswith("ssh://"):
        import urllib.parse

        from ._ssh_curl import _post_via_ssh_curl

        parsed = urllib.parse.urlparse(control_url)
        if not parsed.hostname or not parsed.port:
            raise PeerError(f"malformed ssh URL: {control_url!r}")
        rc, output, error = _post_via_ssh_curl(
            host=parsed.hostname,
            port=parsed.port,
            path="/v1/control",
            body=body,
            timeout_s=timeout_s,
        )
        if rc != 0:
            detail = error.decode("utf-8", "replace") or output.decode(
                "utf-8", "replace"
            )
            raise PeerError(f"ssh+curl control failed (rc={rc}): {detail}")
        try:
            payload = json.loads(output.decode("utf-8"))
        except ValueError as exc:
            raise PeerError(
                f"peer returned malformed control body: {output!r}"
            ) from exc
    else:
        request = urllib.request.Request(
            control_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
                try:
                    payload = json.loads(raw.decode())
                except ValueError as exc:
                    raise PeerError(
                        f"peer returned malformed control body: {raw!r}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise PeerError(
                f"peer returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PeerError(
                f"peer control endpoint unreachable at {control_url}: {exc}"
            ) from exc
    if not isinstance(payload, dict) or payload.get("delivered") is not True:
        raise PeerError(f"peer returned malformed control response: {payload!r}")
    return payload


log = slogging.getLogger(__name__)


class PeerError(RuntimeError):
    """Raised when the peer call cannot be completed (resolution + transport)."""


def __getattr__(name: str):
    """Lazily re-export :class:`PeerTimeoutPending` from ``_peer_timeout``.

    Kept lazy so ``_peer_timeout`` can import ``PeerError`` from this
    module at its own import time without a cycle: nothing here imports
    ``_peer_timeout`` at module load; the symbol resolves on first
    attribute access (``from ..peer import PeerTimeoutPending``).
    """
    if name == "PeerTimeoutPending":
        from ._peer_timeout import PeerTimeoutPending

        return PeerTimeoutPending
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from ._peer_dispatch import (  # noqa: E402
    record_dispatch_safe,
    self_agent_name,
    update_dispatch_safe,
)


def post_turn_to_url(
    url: str,
    text: str,
    *,
    exit_after: bool = False,
    timeout_s: float = 600.0,
    from_agent: str | None = None,
    to_agent: str | None = None,
    conversation_id: str | None = None,
    wait_for_final: bool = True,
) -> str:
    """POST a turn; return its reply or confirmed delivery acceptance.

    Synchronous runners return a body containing ``text``. Asynchronous
    adapters return HTTP 202 plus a canonical ``exchange_id`` and
    ``status_code``. With ``wait_for_final=True`` (the compatibility default),
    this client polls that exchange to a terminal result. With
    ``wait_for_final=False``, it validates and returns the non-final receipt
    immediately so an interactive sender never waits behind a busy agent.
    Raises ``PeerError`` on transport failure, a malformed contract, or a
    terminal non-200 exchange result.

    Mints a dispatch-ledger ``dispatch_id`` and records a row with
    ``status="sent"`` before the POST, stamping the same id into the
    request body so the receiver can correlate. Once an explicitly synchronous
    round-trip resolves the status is moved to ``delivered``. A nonblocking
    submission remains ``sent`` because HTTP 202 is admission, not delivery;
    the exchange resource owns the later final state. Failures move it to
    ``timeout`` (deadline tripped), or ``failed`` (any other transport /
    HTTP error). ``from_agent`` defaults to this container's ``SAC_NAME``.
    """
    if not url.endswith("/v1/turn"):
        raise PeerError(
            f"url must end in /v1/turn (got {url!r}); the runner's inbound "
            "endpoint is the only supported target"
        )

    from .._state.dispatch_ledger import (
        STATUS_DELIVERED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        new_dispatch_id,
    )

    dispatch_id = new_dispatch_id()
    # The requester identity stamped on both the ledger row AND the wire
    # body. Defaults to this container's own name so the receiver's Stop
    # hook can PUSH a completion report back to us — push-feedback, not a
    # special-cased lead. ``None`` only when neither an explicit value nor
    # ``SAC_NAME`` is available (a bare ops script), in which case the
    # receiver has nobody to address and skips the push.
    requester = from_agent if from_agent is not None else self_agent_name()
    record_dispatch_safe(
        from_agent=requester,
        to_agent=to_agent,
        text=text,
        conversation_id=conversation_id,
        dispatch_id=dispatch_id,
    )

    started_at = time.monotonic()
    if url.startswith("ssh://"):
        try:
            reply = _post_turn_via_ssh(
                url,
                text,
                exit_after=exit_after,
                timeout_s=timeout_s,
                dispatch_id=dispatch_id,
                from_agent=requester,
                started_at=started_at,
                wait_for_final=wait_for_final,
            )
        except PeerError as exc:
            from ._peer_timeout import PeerTimeoutPending

            terminal = (
                STATUS_TIMEOUT
                if isinstance(exc, PeerTimeoutPending) or "timeout" in str(exc).lower()
                else STATUS_FAILED
            )
            update_dispatch_safe(dispatch_id, terminal)
            raise
        if wait_for_final:
            update_dispatch_safe(dispatch_id, STATUS_DELIVERED)
        return reply

    visible_text, visible_delivery_id = _bind_visible_delivery(text, dispatch_id)
    turn_body: dict[str, Any] = {
        "text": visible_text,
        "exit_after": bool(exit_after),
        "dispatch_id": dispatch_id,
        "visible_delivery_id": visible_delivery_id,
    }
    if requester is not None:
        turn_body["from_agent"] = requester
    body = json.dumps(turn_body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            http_status = int(resp.status)
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except (
            Exception
        ):  # stx-allow: fallback (reason: defensive — body read may fail)
            err_body = ""
        if exc.code == 504:
            # A 504 means the peer's bounded HTTP wait elapsed — the turn
            # is usually still running, NOT failed. Mark the ledger row
            # 'timeout' and interpret the honest body (PR #169) for the
            # caller instead of surfacing raw JSON; an older peer that
            # returns 504 without the honest shape degrades to a generic
            # "may still be running" message.
            update_dispatch_safe(dispatch_id, STATUS_TIMEOUT)
            raise _interpret_504(err_body, fallback_label=url) from exc
        update_dispatch_safe(dispatch_id, STATUS_FAILED)
        raise PeerError(
            f"peer returned HTTP {exc.code}: {err_body or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        update_dispatch_safe(dispatch_id, STATUS_FAILED)
        raise PeerError(f"peer unreachable at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        update_dispatch_safe(dispatch_id, STATUS_TIMEOUT)
        raise PeerError(f"peer timeout at {url} after {timeout_s:.0f}s") from exc
    from ._peer_exchange import resolve_turn_response

    try:
        reply = resolve_turn_response(
            url,
            payload,
            http_status=http_status,
            timeout_s=max(0.0, timeout_s - (time.monotonic() - started_at)),
            wait_for_final=wait_for_final,
        )
    except PeerError as exc:
        from ._peer_timeout import PeerTimeoutPending

        terminal = (
            STATUS_TIMEOUT if isinstance(exc, PeerTimeoutPending) else STATUS_FAILED
        )
        update_dispatch_safe(dispatch_id, terminal)
        raise
    except (TypeError, ValueError) as exc:
        update_dispatch_safe(dispatch_id, STATUS_FAILED)
        raise PeerError(f"peer returned malformed body: {payload!r}") from exc
    if wait_for_final:
        update_dispatch_safe(dispatch_id, STATUS_DELIVERED)
    return reply


def post_turn(
    agent_name: str,
    text: str,
    *,
    exit_after: bool = False,
    timeout_s: float = 600.0,
    conversation_id: str | None = None,
) -> str:
    """Send a turn to a peer agent by name; return the response ``text``.

    Convenience wrapper that combines :func:`resolve_peer_url` and
    :func:`post_turn_to_url`. Use this from one running agent to drive
    another (a master → workers topology, peer collaboration, etc.).

    Records a dispatch-ledger row with ``to_agent=agent_name`` so a later
    ``list_dispatches(to_agent=...)`` can recall every turn sent to a
    given agent. Pair it with ``agent=`` — the ledger is one fleet-wide
    table since 2026-08-28, so an unscoped recall answers for every agent
    on every host, not just this one.
    """
    url = resolve_peer_url(agent_name)
    return post_turn_to_url(
        url,
        text,
        exit_after=exit_after,
        timeout_s=timeout_s,
        to_agent=agent_name,
        conversation_id=conversation_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interpret_504(err_body: str, *, fallback_label: str) -> PeerError:
    """Return a ``PeerTimeoutPending`` interpreting a 504 response body.

    Parses ``err_body`` as JSON and delegates to
    :func:`_peer_timeout.interpret_timeout_body`. An empty or
    unparseable body still yields a generic "timeout, may still be
    running" interpretation — never a crash, never a raw-JSON leak.

    The return type is annotated ``PeerError`` (the base) so the call
    sites' ``raise ... from exc`` reads cleanly; the concrete object is
    always a :class:`PeerTimeoutPending`.
    """
    from ._peer_timeout import interpret_timeout_body

    body: dict | None
    try:
        parsed = json.loads(err_body) if err_body else None
        body = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        body = None
    return interpret_timeout_body(body, fallback_label=fallback_label)


def _bind_visible_delivery(text: str, delivery_id: str) -> tuple[str, str]:
    """Bind one SAC dispatch identity to transcript-visible prompt text."""
    marker = f"<!-- delivery:{delivery_id} -->"
    return (text if marker in text else f"{text}\n{marker}", delivery_id)


def _post_turn_via_ssh(
    url: str,
    text: str,
    *,
    exit_after: bool,
    timeout_s: float,
    dispatch_id: str | None = None,
    from_agent: str | None = None,
    started_at: float | None = None,
    wait_for_final: bool = True,
) -> str:
    """Dispatch a turn via ``ssh <host> curl ...`` and parse the response.

    Parses ``ssh://host:port/v1/turn``, builds a curl that POSTs to
    ``127.0.0.1:port`` *on the remote*, and pipes the JSON envelope
    through ssh stdin to remote curl stdin. Lets agents stay on
    loopback while peers reach them through the ssh control plane.

    ADR-0015 Stage 2: the ssh argv + remote-curl construction now lives
    in :func:`_ssh_curl._post_via_ssh_curl` and is shared with the
    cross-host ``message:send`` forwarder. The argv shape and the
    ``rc != 0 → PeerError`` semantics here are preserved verbatim; the
    only behavior change is that a ``subprocess.TimeoutExpired`` is now
    surfaced as ``rc=124`` by the helper, which this wrapper still
    maps to the same ``ssh+curl timeout`` ``PeerError``.
    """
    import urllib.parse

    from ._ssh_curl import _post_via_ssh_curl

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise PeerError(f"malformed ssh URL: {url!r}")

    turn_body: dict[str, Any] = {"text": text, "exit_after": bool(exit_after)}
    if dispatch_id is not None:
        visible_text, visible_delivery_id = _bind_visible_delivery(text, dispatch_id)
        turn_body["text"] = visible_text
        turn_body["dispatch_id"] = dispatch_id
        turn_body["visible_delivery_id"] = visible_delivery_id
    if from_agent is not None:
        turn_body["from_agent"] = from_agent
    body = json.dumps(turn_body).encode("utf-8")
    rc, stdout, stderr = _post_via_ssh_curl(
        host=host,
        port=port,
        path="/v1/turn",
        body=body,
        bearer=None,
        timeout_s=timeout_s,
    )
    if rc == 124:
        raise PeerError(f"ssh+curl timeout to {host}:{port} after {timeout_s:.0f}s")
    if rc != 0:
        raise PeerError(
            f"ssh+curl to {host}:{port} failed (rc={rc}): "
            f"{stderr.decode('utf-8', errors='replace').strip()[:300]}"
        )
    stdout_text = stdout.decode("utf-8", errors="replace")
    try:
        # Take the last non-empty line in case .bashrc on the remote
        # printed banners before curl's body.
        lines = [line for line in stdout_text.strip().splitlines() if line.strip()]
        payload = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise PeerError(
            f"ssh+curl to {host}:{port} returned non-JSON: {stdout_text[:300]}"
        ) from exc
    # Over ssh the remote curl (no --fail) returns rc=0 even on a 504, so
    # the HTTP status is invisible — the honest body's status field is the
    # reliable discriminator. When present, interpret it as in-progress.
    if isinstance(payload, dict):
        from ._peer_timeout import TIMEOUT_STATUS

        if payload.get("status") == TIMEOUT_STATUS:
            raise _interpret_504(json.dumps(payload), fallback_label=f"{host}:{port}")
    from ._peer_exchange import resolve_turn_response

    elapsed = 0.0 if started_at is None else time.monotonic() - started_at
    return resolve_turn_response(
        url,
        payload,
        http_status=None,
        timeout_s=max(0.0, timeout_s - elapsed),
        wait_for_final=wait_for_final,
    )


# Agent-name → URL resolution moved to ``_peer_resolve`` under the
# per-file line cap; re-exported here so ``from ...peer import
# resolve_peer_url`` (and the helper imports the tests rely on) keep
# working. ``post_turn`` above calls ``resolve_peer_url`` at call time,
# so the name only needs to be in module globals by then — this
# bottom-of-module import satisfies that without an import cycle
# (``_peer_resolve`` imports ``PeerError`` from here, which is defined
# above before this line runs).
from ._peer_resolve import (  # noqa: E402,F401
    _is_local_host,
    _lookup_bound_port,
    _lookup_instance_endpoint,
    _read_yaml_endpoints,
    _yaml_port_is_auto,
    resolve_peer_url,
)
