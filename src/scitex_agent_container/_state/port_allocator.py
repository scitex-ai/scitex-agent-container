"""Atomic per-agent A2A port allocator — the allocation POLICY.

Per-agent A2A ports are an IPC mechanism between ``sac listen`` (the single
externally-visible host port, default 7878) and each agent's in-process
sidecar runner. Operators should never hand-pick them — collisions are
silent (the second binder fails) and the only sane default is
auto-allocation.

Resolution order at agent_start:

  1. Spec author pinned an explicit int → that int is recorded as the claim.
     Collisions raise (operator intent disagrees with reality — fail loudly).
  2. Spec author set ``port: auto`` (or left a2a unset) → the allocator scans
     ``range_`` ascending and persists the first unused port.

Range defaults to ``(19000, 19999)``. Override via
``~/.scitex/agent-container/config.yaml``::

    a2a:
      port_range: [19000, 19999]

The allocator only owns the **claim**. Actual port binding happens inside the
runner (which exits non-zero if the kernel refuses the bind); ``agent_stop``
calls :func:`release_port` so claims don't leak across runs.

WHERE THE CLAIMS LIVE
=====================
In per-host PostgreSQL via :mod:`scitex_dev.store`, NOT in ``state.db``.
:mod:`.port_allocator_store` is the storage adapter and carries the full
rationale: why the store IDENTITY is the PORT rather than the agent name (the
invariant is ``UNIQUE(port)``), why a released claim is a TOMBSTONE that must
be unhidden rather than treated as held, and why the lookup cost inverts into
an O(n) scan over a range-bounded ledger.

``db_path`` IS GONE from every function here. It named a SQLite file; there is
no file. Callers that threaded it through simply stop, and test isolation
comes from the shared ``pg_schema`` fixture pointing ``SCITEX_STORE_DSN`` at a
throwaway schema.

THE ONE INVARIANT THIS MODULE MUST NOT LOSE
===========================================
    claim(pin) -> release -> re-claim(SAME pin)  MUST SUCCEED.

A pinned agent restarts through exactly that sequence, so a re-claim that
raises means the agent never comes back — every pinned agent on the fleet one
restart from staying down. ``test_port_allocator_pin_reclaim.py`` pins it from
two vantages and states the store behaviour that endangers it.
"""

from __future__ import annotations

import time

from .port_allocator_store import (
    ACTOR as _ACTOR,
)
from .port_allocator_store import (
    STORE_NAME,
    holder_of,
    init_port_schema,
    live_claims,
    open_port_store,
    port_store_target,
    try_claim,
)

# Built-in default range. Tuned to sit above the IANA dynamic range
# floor (49152) is overkill for a single-host loopback IPC channel;
# 19xxx is high enough to avoid common dev ports (8080/8443/9000/9090)
# yet low enough to leave the ephemeral pool intact for outbound
# sockets. Operators override via config.yaml.
DEFAULT_RANGE: tuple[int, int] = (19000, 19999)

__all__ = [
    "DEFAULT_RANGE",
    "STORE_NAME",
    "claim_port",
    "get_port",
    "init_port_schema",
    "list_claims",
    "open_port_store",
    "port_store_target",
    "release_port",
]


def _resolve_range(range_: tuple[int, int] | None) -> tuple[int, int]:
    """Pick the active port range.

    Precedence: explicit ``range_`` arg > ``a2a.port_range`` in
    ``config.yaml`` > module ``DEFAULT_RANGE``. Config-file load is
    tolerant: malformed entries fall through to the default rather
    than blocking agent_start.
    """
    if range_ is not None:
        return range_
    # stx-allow: fallback (reason: config.yaml is operator-edited and
    # may be malformed; a broken range key must not block allocation —
    # fall back to the built-in default.)
    try:
        from .host_config import _default_config_path

        path = _default_config_path()
        if path.is_file():
            import yaml

            raw = yaml.safe_load(path.read_text()) or {}
            a2a_raw = raw.get("a2a") or {}
            pr = a2a_raw.get("port_range")
            if (
                isinstance(pr, (list, tuple))
                and len(pr) == 2
                and all(isinstance(x, int) for x in pr)
                and pr[0] < pr[1]
            ):
                return (int(pr[0]), int(pr[1]))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    return DEFAULT_RANGE


def get_port(agent_name: str) -> int | None:
    """Return the currently-claimed port for ``agent_name``, else ``None``.

    A scan by design — the store identity is the port, so the agent is data.
    See :mod:`.port_allocator_store` for the cost, which is stated there
    rather than hidden.
    """
    store = open_port_store()
    try:
        for port, holder in live_claims(store).items():
            if holder == agent_name:
                return port
        return None
    finally:
        store.close()


def claim_port(
    agent_name: str,
    *,
    range_: tuple[int, int] | None = None,
    explicit: int | None = None,
    explicit_is_pin: bool = True,
) -> int:
    """Atomically claim a free port for ``agent_name``.

    Args:
        agent_name: The spec's ``metadata.name``. Idempotent: a second call
            for the same agent returns the existing port without mutating
            state.
        range_: ``(lo, hi)`` inclusive scan range. Falls back to
            ``config.yaml``'s ``a2a.port_range``, then ``DEFAULT_RANGE``.
        explicit: When set, try to persist this specific port for the agent.
        explicit_is_pin: What a LOST RACE for ``explicit`` MEANS. The two
            origins of an ``explicit`` value are not the same request, and
            conflating them is what made a routine restart fail:

            * ``True`` (an OPERATOR PIN from ``spec.a2a.port``) — the port is
              part of the contract. A foreign holder is a real
              misconfiguration, so raise and make it visible. Silently
              handing back a different port would break the pin.
            * ``False`` (a port WE auto-allocated earlier and are merely
              RE-claiming across a restart) — this is not a pin, it is a
              preference. If it was taken while we were down, a NEW free port
              is the correct answer; failing the launch is not. Falls through
              to the auto scan.

            ``resolve_a2a_port`` MUTATES ``config.a2a.port`` from "auto" to the
            int it claimed, which ERASES that distinction at the call site —
            which is why an *auto*-port agent was traversing the pinned-port
            code on every forced restart. The caller passes the origin back in.

    Returns:
        The port number now bound to ``agent_name``.

    Raises:
        RuntimeError: When no free port remains in ``range_``, or when an
            operator-PINNED ``explicit`` port is held by another agent.
    """
    from scitex_dev.store import ANY_REVISION

    lo, hi = _resolve_range(range_)
    now = time.time()

    store = open_port_store()
    try:
        # ONE read serves the fast path AND the auto scan below. Re-reading
        # per candidate would turn a crowded range into a thousand round trips
        # against PostgreSQL, where SQLite paid only a local file write.
        held = live_claims(store)

        # Idempotent fast path: same agent -> return the existing claim.
        existing = next((p for p, n in held.items() if n == agent_name), None)
        if existing is not None:
            if explicit is None or int(explicit) == existing:
                return existing
            # The operator changed the pin between starts. Release the old
            # claim so the new one can be attempted below.
            store.hide({"port": existing}, expected_revision=ANY_REVISION, actor=_ACTOR)
            held.pop(existing, None)

        if explicit is not None:
            want = int(explicit)
            if try_claim(store, port=want, agent_name=agent_name, now=now):
                return want

            holder = holder_of(store, want)
            if holder == agent_name:
                # We raced OURSELVES (two starts of one agent), which honours
                # claim_port's documented idempotency rather than failing a
                # legitimate re-entry.
                return want

            if explicit_is_pin:
                # An OPERATOR PIN held by someone else is a real
                # misconfiguration. Handing back a different port would
                # silently break the contract the pin exists to state, so
                # fail loud.
                owner = holder if holder is not None else "another agent"
                raise RuntimeError(
                    f"a2a port {want} already claimed by "
                    f"{owner!r}; cannot pin for {agent_name!r}"
                )
            # Not a pin — just the port we happened to hold before this
            # restart, taken while we were down. A fresh port is the correct
            # answer; a dead agent is not. Fall through to the auto scan.

        for candidate in range(lo, hi + 1):
            if candidate in held:
                continue
            if try_claim(store, port=candidate, agent_name=agent_name, now=now):
                return candidate
        raise RuntimeError(
            f"no free a2a port in range [{lo}, {hi}] (all claimed); "
            "extend a2a.port_range in ~/.scitex/agent-container/config.yaml"
        )
    finally:
        store.close()


def release_port(agent_name: str) -> bool:
    """Drop the claim. Idempotent — ``True`` iff a LIVE claim was released.

    Hides rather than deletes, because ``hide`` is the store's only removal.
    The record, its values and its whole history stay readable through
    ``include_hidden=True`` and in the oplog, while every default read treats
    the port as free — so the property the SQLite ``DELETE`` gave ("this agent
    no longer holds a port") is unchanged and only the forgetting stopped.
    ``port_allocator_store.try_claim`` is what makes the tombstone
    re-claimable; that module's docstring says why it has to.
    """
    from scitex_dev.store import ANY_REVISION

    store = open_port_store()
    try:
        for port, holder in live_claims(store).items():
            if holder == agent_name:
                store.hide({"port": port}, expected_revision=ANY_REVISION, actor=_ACTOR)
                return True
        return False
    finally:
        store.close()


def list_claims() -> list[dict]:
    """Every LIVE claim, ascending by port — the shape the CLI renders.

    Used by ``sac agents list``, ``sac ports`` and the listen registry.
    Sorted EXPLICITLY: ``rows()`` returns no order at all, so the SQLite
    ``ORDER BY port`` has to be re-stated here rather than inherited.
    """
    store = open_port_store()
    try:
        claims = [
            {
                "name": str(row.values["name"]),
                "port": int(row.values["port"]),
                "claimed_at": row.values["claimed_at"],
            }
            for row in store.rows()
        ]
    finally:
        store.close()
    return sorted(claims, key=lambda claim: claim["port"])
