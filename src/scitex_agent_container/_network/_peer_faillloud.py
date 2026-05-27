"""Fail-loud helpers for peer-URL resolution (#192).

The pre-#192 resolver had two silent failure modes that produced an
*unbreakable wrong state*:

1. **Silent-local assumption.** When the YAML's ``spec.host`` was empty
   (or stale) and a port happened to resolve from this host's local
   allocator, the resolver returned ``http://127.0.0.1:<port>`` — silently
   assuming the agent runs locally — even when the cross-host registry
   held a fresh ``remote=True`` row placing the agent on a DIFFERENT host.
   This is the clew incident: the lead resolved clew as local when it was
   actually running on Spartan bm001.

2. **Uninformative "is the agent running?".** When NO port resolved, the
   error named neither the last-known host nor when the agent was last
   seen, so the operator had no thread to pull.

These helpers turn both into LOUD, INFORMATIVE failures. The doctrine:
*never silently assume local when the registry says otherwise; never raise
an unresolvable error without naming the last-known placement.*
"""

from __future__ import annotations

from .peer import PeerError

__all__ = [
    "raise_unresolvable_instance",
    "detect_contradicting_remote_instance",
]


def _format_last_known(row: dict) -> str:
    """One-line description of a ``last_known_instance`` row for an error."""
    host = row.get("host") or "<unknown-host>"
    started = row.get("started_at") or "<unknown-time>"
    ended = row.get("ended_at")
    remote = bool(row.get("remote"))
    port = row.get("bound_port")
    if port is None:
        port = row.get("a2a_port")
    state = "ended" if ended else "active per registry (ended_at unset)"
    loc = "remote" if remote else "local"
    port_str = f"port={port}" if port is not None else "port=<none>"
    return (
        f"last known: host={host!r} ({loc}), {port_str}, started_at={started}, {state}"
    )


def raise_unresolvable_instance(agent_name: str, *, port_is_auto: bool) -> None:
    """Raise an INFORMATIVE :class:`PeerError`; never return.

    Called when no live ``/v1/turn`` endpoint resolves for ``agent_name``.
    Consults the cross-host registry's last-known row (active OR ended) so
    the message names the last-known host + timestamp + locality. The
    resolver REFUSES to silently assume the agent is local — the unbreakable
    wrong state #192 was about.

    ``port_is_auto`` selects the base sentence (``port: auto`` agent vs a
    statically-mis-configured one) so the operator sees the right next step.
    """
    last = _last_known(agent_name)
    if last is not None:
        raise PeerError(
            f"agent {agent_name!r}: no live instance resolvable in the "
            f"cross-host registry; {_format_last_known(last)}; current state "
            f"unknown — refusing to assume local. Restart it "
            f"(`sac --on <host> agents start {agent_name}`) or check the "
            f"holding host before sending."
        )
    # No registry history at all — keep the original guidance but stay loud
    # about the locality refusal.
    if port_is_auto:
        raise PeerError(
            f"agent {agent_name!r} has port: auto and no bound port recorded "
            f"in the cross-host registry, and no prior instance row exists; "
            f"the agent has never run on a host this lead knows about — "
            f"start it before sending. Refusing to assume local."
        )
    raise PeerError(
        f"agent {agent_name!r} has no spec.a2a.port and no registry history — "
        f"add a port to its YAML to enable inbound /v1/turn, then start it."
    )


def detect_contradicting_remote_instance(
    agent_name: str,
    *,
    resolved_local: bool,
) -> dict | None:
    """Return a fresh ``remote=True`` row that contradicts a local resolution.

    The resolver only consults ``_lookup_instance_endpoint`` when no port
    resolved from the YAML / local allocator. That leaves a hole: a STALE
    local-allocator port can make the resolver land on
    ``http://127.0.0.1`` while a FRESH ``remote=True`` instances row places
    the agent on another host. Before returning a local URL, the resolver
    asks this helper whether such a contradicting remote row exists.

    Returns the contradicting row when ``resolved_local`` is True AND the
    latest active instance for ``agent_name`` is ``remote=True`` on a host
    that is not this one. Returns ``None`` when there is no contradiction
    (no row, the active row is local, or the resolution was already
    remote). Best-effort registry read — never raises (a registry glitch
    must not turn a working send into a crash).
    """
    if not resolved_local:
        return None
    try:
        from .._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r.get("name") == agent_name]
    except Exception:  # stx-allow: fallback (reason: best-effort contradiction probe — a registry read glitch must not crash a send that would otherwise work)
        return None
    if not rows:
        return None
    row = rows[0]  # started_at DESC → newest active first
    if bool(row.get("remote")):
        return row
    return None


def _last_known(agent_name: str) -> dict | None:
    """Best-effort fetch of the last-known instance row for ``agent_name``."""
    try:
        from .._state.state_db import last_known_instance

        return last_known_instance(agent_name)
    except Exception:  # stx-allow: fallback (reason: best-effort evidence read for the error message — a registry glitch must not mask the underlying PeerError)
        return None
