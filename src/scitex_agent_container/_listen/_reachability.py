"""REGISTERED is not REACHABLE — the inbox-subscriber observation.

A row in ``GET /agents`` used to say only what the registry DECLARED: a
name, a pid, a port, a start time, a group. All of that can be true of an
agent whose inbox adapter is not attached to the channel bus — an agent
that will silently swallow every ``a2a_send`` aimed at it, because the
broker fans out to zero subscribers.

That conflation was load-bearing. The fleet constitution says "before
handing work to another party, confirm it is alive and able to act", and
``a2a_peers`` is the tool provided to do exactly that. An agent could
confirm via ``a2a_peers``, be told the peer was running and ``active``,
send, and reach nobody. A liveness signal that reports alive-and-able for
a deaf agent is worse than no signal, because it is trusted.

So this module keeps the two facts DISTINCT:

* **registered** — the registry has a row (pid, port, groups). Unchanged;
  still reported. It is a declaration.
* **reachable** — the ``sac listen`` broker has a live SSE subscriber on
  that agent's inbox stream. It is an OBSERVATION, and it is the only one
  that predicts whether a message will actually wake them.

Three states, never two
-----------------------
``inbox_reachable`` is deliberately ternary. Collapsing "I could not
check" into either "fine" or "dead" is the class of bug this whole module
exists to kill:

* :data:`REACHABLE`   — observed >= 1 live subscriber. A send will wake them.
* :data:`UNREACHABLE` — observed exactly 0 subscribers on a bus we CAN see.
  This is EVIDENCE of non-delivery, not absence of evidence, so an
  ``a2a_send`` to this agent fails loudly (see ``_mcp/_channel_send_errors``).
* :data:`UNKNOWN`     — we cannot observe this agent's bus at all (it lives
  on another host, whose broker is a different process). NOT a failure and
  NOT a success. Never rendered as either.

And a hard rule that outlives this file: ``UNREACHABLE`` must NEVER be
wired to anything destructive. It says an inbox adapter is detached; it
does NOT say the agent is dead. Auto-restarting on it would destroy a
healthy session — the exact "destroy on a negative signal you did not
directly observe" failure this fleet has already paid for.
"""

from __future__ import annotations

from typing import Any, Mapping

# ``inbox_reachable`` values.
REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

__all__ = [
    "REACHABLE",
    "UNKNOWN",
    "UNREACHABLE",
    "annotate_reachability",
    "annotate_rows",
]


def _is_locally_observable(row: Mapping[str, Any], local_host: str | None) -> bool:
    """Can THIS listen's broker answer for ``row``'s inbox?

    Mirrors the locality rule the publish path already uses
    (:func:`_state.state_db_nodes.is_local_node`): a node whose host we
    cannot distinguish from our own — including one that declares no host
    at all — is served by the LOCAL broker, so the local subscriber count
    is authoritative for it.

    A row that declares a host we can see is NOT ours is served by a
    different ``sac listen`` process with a different in-memory broker.
    We have no window into it, so we must say :data:`UNKNOWN` rather than
    invent a zero — a fabricated zero would be a false accusation of
    deafness against a perfectly reachable remote agent.
    """
    host = row.get("host")
    if not isinstance(host, str) or not host:
        return True  # no host declared → the local publish path serves it
    if not local_host:
        return False  # we don't know who WE are → cannot claim locality
    return host == local_host


def annotate_reachability(
    row: Mapping[str, Any],
    *,
    subscriber_counts: Mapping[str, int],
    local_host: str | None,
) -> dict[str, Any]:
    """Add ``inbox_subscribers`` + ``inbox_reachable`` to one registry row.

    ``subscriber_counts`` is a snapshot of the local broker
    (:meth:`a2a._inbox_bus.Broker.subscriber_counts`) — an in-memory dict,
    so this costs no I/O per row and cannot stall ``GET /agents``.

    Idempotent and non-destructive: every pre-existing field (``pid``,
    ``groups``, ``started_at``, …) is preserved untouched. We ADD the
    observation next to the declaration; we do not overwrite or reinterpret
    the declaration.
    """
    out = dict(row)
    name = row.get("name")
    if not isinstance(name, str) or not name:
        # No usable key for the broker → we genuinely cannot look it up.
        out["inbox_subscribers"] = None
        out["inbox_reachable"] = UNKNOWN
        return out

    if not _is_locally_observable(row, local_host):
        out["inbox_subscribers"] = None
        out["inbox_reachable"] = UNKNOWN
        return out

    count = int(subscriber_counts.get(name, 0))
    out["inbox_subscribers"] = count
    out["inbox_reachable"] = REACHABLE if count >= 1 else UNREACHABLE
    return out


def annotate_rows(
    rows: list[dict[str, Any]],
    *,
    subscriber_counts: Mapping[str, int],
    local_host: str | None,
) -> list[dict[str, Any]]:
    """Apply :func:`annotate_reachability` across a ``GET /agents`` list."""
    return [
        annotate_reachability(
            row, subscriber_counts=subscriber_counts, local_host=local_host
        )
        for row in rows
    ]
