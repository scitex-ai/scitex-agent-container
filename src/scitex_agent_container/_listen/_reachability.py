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
    "resolve_annotation_host",
]


def resolve_annotation_host(app_state: Any) -> str | None:
    """The host identity to weigh rows against, for BOTH annotate paths.

    ``create_app`` declares ``local_host`` and defaults it to ``None``, and
    the one production caller (``cli_pkg.listen_cmds``) never passes it. So
    ``app.state.local_host`` is ``None`` in production, always. Read alone it
    makes :func:`_is_locally_observable` answer ``False`` for every row that
    carries any host evidence, and every such row is annotated ``unknown`` —
    including rows on THIS host, whose subscriber count we can see perfectly.

    The env resolver is therefore not a nicety, it is the only thing that
    supplies an identity in production. It lives here, in one function, for a
    measured reason: the fallback was added to the single-row path in #1174
    and NOT to the list path, so ``GET /agents`` — the endpoint ``a2a_peers``
    reads and the one every "nobody is reachable" report comes through — kept
    returning ``unknown`` for the whole fleet after the fix shipped. Two call
    sites asking one question is how half a fix looks like a whole one.

    Returns ``None`` only if the resolver itself cannot answer, which
    ``_is_locally_observable`` correctly reads as "we don't know who WE are"
    and degrades to ``unknown`` — never to a fabricated ``unreachable``.
    """
    from .._state.state_db import _resolve_host

    return getattr(app_state, "local_host", None) or _resolve_host(None)


def _is_locally_observable(row: Mapping[str, Any], local_host: str | None) -> bool:
    """Can THIS listen's broker answer for ``row``'s inbox?

    Mirrors the locality rule the publish path already uses
    (:func:`_state.state_db_nodes.is_local_node`): a node whose host we
    cannot distinguish from our own is served by the LOCAL broker, so the
    local subscriber count is authoritative for it.

    A row that declares a host we can see is NOT ours is served by a
    different ``sac listen`` process with a different in-memory broker.
    We have no window into it, so we must say :data:`UNKNOWN` rather than
    invent a zero — a fabricated zero would be a false accusation of
    deafness against a perfectly reachable remote agent.

    A MISSING ``host`` IS NOT EVIDENCE OF LOCALITY. It used to be treated
    as such, and that is how the false accusation above got made anyway:
    see :func:`_host_from_turn_url` for the measured case. The row's
    ``turn_url`` is consulted as a fallback, and only a row carrying
    NEITHER field falls through to "local" — which is the honest reading,
    because then there genuinely is no host evidence to weigh.
    """
    host = row.get("host") or _host_from_turn_url(row)
    if not isinstance(host, str) or not host:
        return True  # no host evidence at all → the local publish path serves it
    if not local_host:
        return False  # we don't know who WE are → cannot claim locality
    return host == local_host


def _host_from_turn_url(row: Mapping[str, Any]) -> str | None:
    """Recover the row's host from ``turn_url`` when ``host`` is absent.

    A row with no ``host`` key used to be treated as LOCAL outright, and
    that is the hole: absence of a declaration is absence of INFORMATION,
    not evidence of locality. Reading it as local lets the local broker's
    zero subscriber count stand as authoritative for an agent living on
    another machine — and a fabricated zero is exactly the false
    accusation the rest of this module is written to avoid.

    Measured 2026-08-19. ``agent_status paper-scitex-clew``, run on
    compute-04, returned a row with NO ``host`` key and::

        turn_url : http://ywata-note-win:19012/v1/turn

    The agent was alive on compute-02 with a heartbeat 2 seconds old.
    Because the row looked local, it was annotated ``unreachable``
    instead of ``unknown``; ``classify_fault``'s cross-host guard
    (``inbox_reachable == UNKNOWN -> None``) therefore never fired, and
    the verdict read "NOT RUNNING ... the probe SUCCEEDED, so this is
    real absence, not a failed look" about a healthy agent on a host
    this daemon cannot see. The operator and I both believed it.

    So the fix is not new evidence — the row already carried the host in
    its ``turn_url``. It was simply not consulted.

    Returns ``None`` when there is no usable hostname, which preserves
    the original "no evidence → local" behaviour for genuinely local
    rows that declare neither field.
    """
    url = row.get("turn_url")
    if not isinstance(url, str) or not url:
        return None
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or None
    except Exception:  # stx-allow: fallback (reason: a malformed turn_url must not break annotation; falling through to None restores the prior behaviour for this row)
        return None


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
