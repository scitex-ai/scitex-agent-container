"""Outbound peer-to-peer client for the claude-session inbound endpoint.

Layer 3 of the orochi-consumption rollout. Layer 2 made it possible to
spawn + manage a remote agent that listens on ``POST /v1/turn``. This
module is the *outbound* side: an ergonomic helper for one runner (or
ops script) to drop a new turn onto another agent's persistent SDK
conversation.

Two surfaces:

* ``post_turn_to_url(url, text, *, exit_after=False, timeout_s=600.0)``
  — low-level. Posts the JSON envelope to a known URL, returns the
  response ``text`` string.

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
import logging
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "post_turn",
    "post_turn_to_url",
    "resolve_peer_url",
    "PeerError",
    "PeerTimeoutPending",
]

log = logging.getLogger(__name__)


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
) -> str:
    """POST a single turn to a known ``/v1/turn`` URL; return the ``text`` string.

    Raises ``PeerError`` on transport failure or non-200 status with the
    server's error message included.

    Mints a dispatch-ledger ``dispatch_id`` and records a row with
    ``status="sent"`` before the POST, stamping the same id into the
    request body so the receiver can correlate. Once the round-trip
    resolves the status is moved to ``delivered`` (clean reply),
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
    record_dispatch_safe(
        from_agent=from_agent if from_agent is not None else self_agent_name(),
        to_agent=to_agent,
        text=text,
        conversation_id=conversation_id,
        dispatch_id=dispatch_id,
    )

    if url.startswith("ssh://"):
        try:
            reply = _post_turn_via_ssh(
                url,
                text,
                exit_after=exit_after,
                timeout_s=timeout_s,
                dispatch_id=dispatch_id,
            )
        except PeerError as exc:
            terminal = (
                STATUS_TIMEOUT if "timeout" in str(exc).lower() else STATUS_FAILED
            )
            update_dispatch_safe(dispatch_id, terminal)
            raise
        update_dispatch_safe(dispatch_id, STATUS_DELIVERED)
        return reply

    body = json.dumps(
        {
            "text": text,
            "exit_after": bool(exit_after),
            "dispatch_id": dispatch_id,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
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
    if not isinstance(payload, dict) or "text" not in payload:
        update_dispatch_safe(dispatch_id, STATUS_FAILED)
        raise PeerError(f"peer returned malformed body: {payload!r}")
    update_dispatch_safe(dispatch_id, STATUS_DELIVERED)
    return str(payload["text"])


def resolve_peer_url(agent_name: str) -> str:
    """Resolve the ``/v1/turn`` URL for a named agent.

    Looks up the agent's YAML via the standard discovery chain
    (project-local → ``~/.scitex/agent-container/agents/`` → env →
    fleet dirs), reads ``spec.a2a.{host,port}`` and ``spec.host``,
    and returns the URL the caller should POST to.

    When the YAML pins ``spec.a2a.port: auto`` the actual bound port
    isn't in the YAML — it lives in ``state.db``'s ``a2a_ports`` table
    where the port allocator persists the claim at agent_start.  We
    consult that table by ``agent_name`` to discover the real port.
    See foundation-polish bug 1.

    For **cross-host** agents (``spec.host`` is set to a non-local peer
    name) the returned URL is a synthetic ``ssh://<host>:<port>/v1/turn``
    form that :func:`post_turn_to_url` recognises and dispatches via
    ``ssh <host> curl http://127.0.0.1:<port>/...``. This way the agent
    can keep ``spec.a2a.host: 127.0.0.1`` (more secure) and remote
    callers still reach it through the ssh control plane — no LAN
    exposure required, no DNS resolution needed for ssh aliases.

    The same ``spec.host`` field is consulted by ``sac start`` for
    dispatch, so post-turn cannot disagree about where the agent
    lives. The legacy ``spec.remote`` block was deleted in WI-6
    (handoff §6, 2026-05-20).
    """
    from ..config._resolve import resolve_config

    try:
        yaml_path = resolve_config(agent_name)
    except FileNotFoundError as exc:
        raise PeerError(str(exc)) from exc

    a2a_host, a2a_port, dest_host = _read_yaml_endpoints(yaml_path)
    if a2a_port is None:
        a2a_port = _lookup_bound_port(agent_name)
    if a2a_port is None:
        if _yaml_port_is_auto(yaml_path):
            raise PeerError(
                f"agent {agent_name!r} has port: auto and no bound port "
                "recorded in registry; is the agent running?"
            )
        raise PeerError(
            f"agent {agent_name!r} has no spec.a2a.port — add a port to "
            "its YAML to enable inbound /v1/turn"
        )
    if dest_host and not _is_local_host(dest_host):
        # Tunnel via ssh — agent's a2a.host can stay loopback (default).
        return f"ssh://{dest_host}:{a2a_port}/v1/turn"
    # Local agent (spec.host empty or pointing at this machine).
    host = a2a_host or "127.0.0.1"
    return f"http://{host}:{a2a_port}/v1/turn"


def _is_local_host(dest_host: str) -> bool:
    """Return True iff ``dest_host`` names the current machine.

    Consults host_config's canonical hostname so an agent pinned to
    its own host is reached via http://127.0.0.1, not via ssh-to-self.
    Any resolution failure raises — we do not silently treat unknown
    hosts as local (would mask config drift).
    """
    from .._state.host_config import load as load_host_config

    cfg = load_host_config()
    canonical = cfg.canonical_host()
    return dest_host == canonical or dest_host in cfg.host.aliases


def _lookup_bound_port(agent_name: str) -> int | None:
    """Return the port the allocator persisted for ``agent_name``, else None.

    The YAML may say ``spec.a2a.port: auto`` (or omit ``port`` entirely)
    when the spec author wants the runtime to pick a free port. The
    actual port is recorded in the ``a2a_ports`` table in ``state.db``
    by :func:`_state.port_allocator.claim_port` at agent_start. The
    peer client consults the same table so it can talk to an
    auto-port agent without having to re-parse + reproduce the
    allocator's logic.

    Failure modes (registry missing, schema not yet created, sqlite
    locked) degrade to ``None`` so the caller raises the same "no
    port recorded" PeerError it would for a static-port misconfig.
    """
    try:
        from .._state.port_allocator import get_port

        return get_port(agent_name)
    except Exception:  # stx-allow: fallback (reason: best-effort lookup — caller raises a clear PeerError when None)
        return None


def _yaml_port_is_auto(yaml_path: str) -> bool:
    """Return True iff ``spec.a2a.port`` is the literal string ``"auto"``.

    Used to decide which PeerError to raise when no bound port is
    available: an auto-port spec with no registry entry means "agent
    isn't running", while a missing port means "the spec is incomplete".
    Best-effort — any IO / parse failure returns False.
    """
    try:
        from pathlib import Path

        import yaml as _yaml

        raw = _yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:  # stx-allow: fallback (reason: best-effort detection; falls through to generic no-port error)
        return False
    spec = (raw.get("spec") or {}) if isinstance(raw, dict) else {}
    a2a = spec.get("a2a") or {}
    port = a2a.get("port") if isinstance(a2a, dict) else None
    return isinstance(port, str) and port.strip().lower() == "auto"


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
    another (orochi master → workers, peer collaboration, etc.).

    Records a dispatch-ledger row with ``to_agent=agent_name`` so a later
    ``list_dispatches(to_agent=...)`` can recall every turn sent to a
    given agent.
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


def _post_turn_via_ssh(
    url: str,
    text: str,
    *,
    exit_after: bool,
    timeout_s: float,
    dispatch_id: str | None = None,
) -> str:
    """Dispatch a turn via ``ssh <host> curl ...`` and parse the response.

    Parses ``ssh://host:port/v1/turn``, builds a curl that POSTs to
    ``127.0.0.1:port`` *on the remote*, and pipes the JSON envelope
    through ssh stdin to remote curl stdin. Lets agents stay on
    loopback while peers reach them through the ssh control plane.
    """
    import subprocess
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise PeerError(f"malformed ssh URL: {url!r}")

    remote_curl = (
        f"curl -sS --max-time {int(timeout_s)} "
        "-X POST -H 'Content-Type: application/json' -d @- "
        f"http://127.0.0.1:{port}/v1/turn"
    )
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        host,
        remote_curl,
    ]
    turn_body: dict[str, Any] = {"text": text, "exit_after": bool(exit_after)}
    if dispatch_id is not None:
        turn_body["dispatch_id"] = dispatch_id
    body = json.dumps(turn_body)
    try:
        proc = subprocess.run(
            ssh_cmd,
            input=body,
            capture_output=True,
            text=True,
            timeout=timeout_s + 15,
        )
    except subprocess.TimeoutExpired as exc:
        raise PeerError(
            f"ssh+curl timeout to {host}:{port} after {timeout_s:.0f}s"
        ) from exc
    if proc.returncode != 0:
        raise PeerError(
            f"ssh+curl to {host}:{port} failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:300]}"
        )
    try:
        # Take the last non-empty line in case .bashrc on the remote
        # printed banners before curl's body.
        lines = [
            line for line in (proc.stdout or "").strip().splitlines() if line.strip()
        ]
        payload = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise PeerError(
            f"ssh+curl to {host}:{port} returned non-JSON: {(proc.stdout or '')[:300]}"
        ) from exc
    # Over ssh the remote curl (no --fail) returns rc=0 even on a 504, so
    # the HTTP status is invisible — the honest body's status field is the
    # reliable discriminator. When present, interpret it as in-progress.
    if isinstance(payload, dict):
        from ._peer_timeout import TIMEOUT_STATUS

        if payload.get("status") == TIMEOUT_STATUS:
            raise _interpret_504(json.dumps(payload), fallback_label=f"{host}:{port}")
    if not isinstance(payload, dict) or "text" not in payload:
        raise PeerError(f"peer returned malformed body: {payload!r}")
    return str(payload["text"])


def _read_yaml_endpoints(yaml_path: str) -> tuple[str | None, int | None, str | None]:
    """Return ``(a2a_host, a2a_port, dest_host)`` from a v3 YAML file.

    ``dest_host`` is the agent's destination peer name (the value of
    ``spec.host``). It is the same lookup key used by cross-host
    dispatch, so peer routing and start dispatch agree on a single
    field. SSH alias resolution happens at the SSH layer
    (``~/.ssh/config``), not here.

    Best-effort: any IO / parse failure produces ``(None, None, None)``.
    """
    try:
        from pathlib import Path

        import yaml as _yaml

        v3 = _yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:  # stx-allow: fallback (reason: malformed YAML degrades to "no endpoints" — caller raises a clear PeerError)
        return (None, None, None)
    spec = (v3.get("spec") or {}) if isinstance(v3, dict) else {}
    a2a: dict[str, Any] = spec.get("a2a") or {}
    a2a_port = a2a.get("port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        a2a_port = None
    a2a_host = a2a.get("host")
    if not isinstance(a2a_host, str) or not a2a_host.strip():
        a2a_host = None
    # spec.host (HostsSpec) is the single source of truth for the
    # destination peer. Can be empty (local), a string (one host),
    # or a list (priority chain — last entry wins for routing).
    raw_host = spec.get("host")
    dest_host: str | None = None
    if isinstance(raw_host, str) and raw_host.strip():
        dest_host = raw_host.strip()
    elif isinstance(raw_host, list) and raw_host:
        last = raw_host[-1]
        if isinstance(last, str) and last.strip():
            dest_host = last.strip()
    return (a2a_host, a2a_port, dest_host)
