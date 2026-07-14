"""Host-brokered peer lookup for the send read path (in-container blindness fix).

The bug
-------
Inside an apptainer SIF, ``$HOME`` is ``/home/agent`` (not the operator's
home) and ``SCITEX_AGENT_CONTAINER_STATE_DB`` points at a PER-AGENT bridge
DB (e.g. ``/state/<name>/state.db``). So ``open_db(None)`` resolves to a
private, effectively empty store that holds NO rows for any other agent.

Every local-DB read in the send path therefore comes back empty, and the
caller renders that emptiness as *death*. Measured 2026-07-14 from inside
the ``scitex-agent-container`` SIF::

    resolve_send_endpoint("scitex-scholar")
        -> a2a_port=None, source="none", row=None
    send_to_agent("scitex-scholar")
        -> {"status": "error", "error": "agent 'scitex-scholar' not running",
            "diagnosis": {"registry_status": "stopped", "pid": null,
                          "a2a_port": null, "boot_complete": false}}

…while the HOST's shared registry, at the same moment, held a live row for
that agent (``pid=1777985  a2a_port=19037  ended_at=NULL``) and the agent
had messaged the caller 90 seconds earlier. The lookup was never performed
against the real fleet registry — and "I could not check" was rendered as
"the agent is stopped".

The fix
-------
Reuse the EXISTING in-SIF broker seam. :func:`agent_status` already solves
this correctly: when it detects a SIF it proxies ``GET /agents/<name>/status``
to the host's ``sac listen`` via
:func:`_lifecycle._in_sif_http_client.host_listen_call` (see
``cli_pkg/status_cmds.py::_status_via_host_listen``), which reads the real
fleet registry on the bare host. This module routes the SEND path's peer
lookup through that same door — same client, same URL, same bearer, same
server-side auth gate. No second mechanism, no reimplemented client.

Fail-loud / never-fabricate-death invariants
--------------------------------------------
These are the whole point of the module; they are not decoration.

* **Broker unreachable → UNKNOWN, never DEAD.** A transport failure raises
  :class:`PeerLookupUnavailable`. We do NOT fall back to the blind local
  read, because that read is precisely what reports a live agent as
  "stopped". Reporting a live agent dead because we could not ask IS the
  bug.
* **A refusal is not a death certificate.** A 403 (ACL) / 5xx / unparseable
  body means we were prevented from learning the answer — also
  :class:`PeerLookupUnavailable` (UNKNOWN).
* **Only the HOST may pronounce an agent absent.** A 404 from the host is
  the one definitive negative: the fleet registry has no such agent.
* **Hints are not verdicts.** The host's ``status`` field (e.g.
  ``"startup_failed"``) is carried through as a HINT ONLY. Those markers go
  stale and are never reconciled — measured 2026-07-14, ``scitex-writer``
  reported ``startup_failed`` from a marker ~2 days old while its host row
  held ``pid=1772715 / ended_at=NULL`` and it answered an a2a message that
  same turn. A stale marker MUST NOT render a live agent unreachable.

Why the caution is asymmetric: the remedy for a "dead" verdict is
DESTRUCTIVE (``--force --fresh``). A false red kills a healthy, working
agent; a false green merely wastes a round-trip. When the evidence does not
support a conclusion, refuse to conclude.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple

from ._send_resolve import ResolvedEndpoint

__all__ = [
    "BrokeredPeer",
    "PeerLookupUnavailable",
    "lookup_peer_via_host",
    "resolve_send_endpoint_via_host",
    "should_broker_peer_lookup",
]

# The peer lookup is a single small GET against a loopback server; it must
# never be the thing that makes a send feel slow. Deliberately far below the
# 30s default of the shared client.
_DEFAULT_TIMEOUT_S = 10.0


class PeerLookupUnavailable(RuntimeError):
    """The peer's state could NOT BE READ — the verdict is UNKNOWN, not DEAD.

    Raised when the host broker could not be asked, or answered in a way that
    withholds the fact we needed (transport failure, ACL refusal, 5xx,
    unparseable body).

    This is emphatically **not** "the agent is stopped". The caller MUST
    surface it as unknown-state and MUST NOT fall back to the blind local
    read, whose empty result would masquerade as death.

    Attributes:
        url: The URL that was attempted (``None`` when the failure happened
            before a URL could be built).
        http_status: The host's HTTP status, when one was received.
        kind: The host's structured failure tag (e.g. ``"acl_deny"``), when
            the body carried one.
        body: The parsed response body, verbatim, when one was received.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        http_status: int | None = None,
        kind: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.http_status = http_status
        self.kind = kind
        self.body = body


class BrokeredPeer(NamedTuple):
    """What the HOST fleet registry knows about a peer.

    ``known`` is ``False`` only for the host's definitive 404 — the fleet
    registry has no agent by that name. Every other "we don't know" case
    raises :class:`PeerLookupUnavailable` instead of landing here, so a
    ``BrokeredPeer`` always represents an ANSWER, never a shrug.

    ``a2a_port`` is the durable port claim the host holds for the agent
    (released only at ``agent_stop`` / ``--force``), so a non-``None`` value
    is real evidence the agent is up.

    ``host_status`` is the host's ``status`` field (e.g. ``"startup_failed"``)
    — a HINT for the operator, NEVER a liveness verdict. See the module
    docstring: these markers go stale and nothing reconciles them.
    """

    known: bool
    a2a_port: int | None
    host: str | None
    host_status: str | None
    body: dict


def should_broker_peer_lookup() -> bool:
    """True iff the peer lookup must be brokered to the host ``sac listen``.

    Mirrors the decision ``agent_spawn`` and ``agent_status`` already make:
    in a SIF *and* the apptainer runtime injected a listen URL to broker to.

    The ``SAC_LISTEN_BASE_URL`` half of the predicate is not belt-and-braces
    — it is load-bearing. ``sac-from-sac`` / bare-host shells can carry a
    stale ``SINGULARITY_CONTAINER`` with no host listen to talk to, and
    brokering there would turn a working local read into a stillborn one.
    ``status_cmds.py`` learned this the hard way (PR#316); we reuse the same
    guard rather than re-discover it.

    Resolved through :func:`_env.getenv` — the SAME resolver
    :func:`_in_sif_http_client.host_listen_call` uses — so the guard and the
    client can never disagree about whether a base URL exists.
    """
    from .._env import getenv
    from .._lifecycle._in_sif_broker import is_in_sif

    if not is_in_sif():
        return False
    return bool((getenv("LISTEN_BASE_URL", "") or "").strip())


def _host_from_turn_url(turn_url: Any) -> str | None:
    """Extract the hostname from the host's ``turn_url``, else ``None``.

    ``GET /agents/<name>/status`` carries the agent's endpoint as a derived
    ``turn_url`` (``http://<host>:<port>/v1/turn``) rather than a bare host
    field, so that is where the peer's host comes from.
    """
    if not isinstance(turn_url, str) or not turn_url:
        return None
    from urllib.parse import urlsplit

    try:
        return urlsplit(turn_url).hostname or None
    except ValueError:
        # stx-allow: fallback (reason: a malformed turn_url costs us only the
        # host field — the caller falls back to the local canonical host. It
        # must never escalate into a fabricated "agent is dead".)
        return None


def _coerce_port(value: Any) -> int | None:
    """Return a positive int port, else ``None`` (``bool`` rejected explicitly)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def lookup_peer_via_host(
    name: str,
    *,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
) -> BrokeredPeer:
    """Ask the host ``sac listen`` what it knows about ``name``.

    Routes ``GET /agents/<name>/status`` through
    :func:`_lifecycle._in_sif_http_client.host_listen_call` — the same client,
    URL and bearer ``agent_status`` uses. The host's ``BearerAuthMiddleware``
    and lineage ACL remain the authority; nothing here weakens or bypasses
    them, and a refusal is surfaced (as UNKNOWN), never worked around.

    Returns:
        :class:`BrokeredPeer` — an ANSWER from the host. ``known=False`` only
        for the host's definitive 404.

    Raises:
        PeerLookupUnavailable: When the answer could not be obtained
            (transport failure, ACL refusal, 5xx, non-dict body). The verdict
            is UNKNOWN — the caller must not render it as "stopped".
    """
    from .._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )

    try:
        http_status, body = host_listen_call(
            "GET",
            f"/agents/{name}/status",
            base_url=base_url,
            bearer=bearer,
            timeout_s=timeout_s,
            opener=opener,
        )
    except HostListenTransportError as exc:
        raise PeerLookupUnavailable(
            f"cannot determine whether agent {name!r} is running: the host "
            f"listen broker is unreachable ({exc}). Verdict: UNKNOWN — this "
            f"is NOT evidence the agent is stopped, and it must not be acted "
            f"on as though it were. Refusing to fall back to the "
            f"container-local registry, which cannot see any peer and would "
            f"report every one of them dead.",
            url=exc.url,
        ) from exc

    if http_status == 404:
        # The one definitive negative: the HOST — which can see the whole
        # fleet — has no agent by this name.
        return BrokeredPeer(
            known=False,
            a2a_port=None,
            host=None,
            host_status=None,
            body=body if isinstance(body, dict) else {},
        )

    if not (200 <= http_status < 300) or not isinstance(body, dict):
        kind = body.get("kind") if isinstance(body, dict) else None
        raise PeerLookupUnavailable(
            f"cannot determine whether agent {name!r} is running: the host "
            f"listen answered GET /agents/{name}/status with HTTP "
            f"{http_status} (kind={kind!r}). Verdict: UNKNOWN — we were "
            f"prevented from learning the agent's state, which is NOT the "
            f"same as learning that it is stopped.",
            http_status=http_status,
            kind=kind if isinstance(kind, str) else None,
            body=body,
        )

    raw_status = body.get("status")
    return BrokeredPeer(
        known=True,
        a2a_port=_coerce_port(body.get("a2a_port")),
        host=_host_from_turn_url(body.get("turn_url")),
        host_status=raw_status if isinstance(raw_status, str) else None,
        body=body,
    )


def resolve_send_endpoint_via_host(
    name: str,
    *,
    current_host: str,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
) -> tuple[ResolvedEndpoint, BrokeredPeer]:
    """Brokered twin of :func:`_send_resolve.resolve_send_endpoint`.

    Returns the SAME :class:`ResolvedEndpoint` shape the local resolver
    returns, so the caller's downstream logic is identical on both paths;
    only ``source`` differs, which is deliberate — provenance must be visible
    in the payload, so nobody has to guess whether a verdict came from the
    blind container DB or the real fleet registry.

    ``source`` values:
      * ``"host_broker"`` — the host holds a live port claim for the agent.
      * ``"host_broker_no_port"`` — the host knows the agent but holds no
        port claim for it.
      * ``"host_broker_unknown_agent"`` — the host's 404: no such agent.

    The :class:`BrokeredPeer` is returned alongside so the caller can thread
    the host's facts into the diagnosis WITHOUT a second HTTP round-trip.

    Raises:
        PeerLookupUnavailable: propagated from :func:`lookup_peer_via_host`.
            The caller must render this as UNKNOWN, never as "not running".
    """
    peer = lookup_peer_via_host(
        name,
        base_url=base_url,
        bearer=bearer,
        timeout_s=timeout_s,
        opener=opener,
    )
    if not peer.known:
        return (
            ResolvedEndpoint(
                a2a_port=None,
                host=current_host,
                source="host_broker_unknown_agent",
                row=None,
            ),
            peer,
        )
    source = "host_broker" if peer.a2a_port is not None else "host_broker_no_port"
    return (
        ResolvedEndpoint(
            a2a_port=peer.a2a_port,
            host=peer.host or current_host,
            source=source,
            row=None,
        ),
        peer,
    )
