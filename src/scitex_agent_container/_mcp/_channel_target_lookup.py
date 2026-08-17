"""What the send path asks the REGISTRY about a target that reached nobody.

Extracted from :mod:`._channel_tools` (at the per-file cap), and cohesive on its
own terms: these are the pure projections over a ``GET /agents`` body that turn
one ambiguous number into an actionable one.

``delivered_subscriber_count == 0`` has THREE causes which demand DIFFERENT
responses, and the count is identical in all three:

===============================  ==========================================
cause                            what the sender must do
===============================  ==========================================
adapter detached, agent LIVE     WAIT — the row replays on their reconnect
agent NOT RUNNING                DO NOT WAIT — nothing will reconnect
name never registered (a typo)   FIX THE NAME — nothing is queued at all
===============================  ==========================================

The fleet has paid for both collapses. The typo case cost scitex-dev a day of
messages addressed to ``sac-04`` (2026-08-09), every one answered ``200`` with
``durably_queued=true``. The not-running case is the same shape one layer over:
measured 2026-08-12, **9 of 15** registered rows on this host were stopped
agents, and a sender hitting any of them was told — in bold, by
``NO_SUBSCRIBER_REMEDY`` — that the message would replay on a reconnect that no
longer had a process to happen in.

Everything here is a PURE function over rows, so the rules are testable without
a listen daemon, and every one of them is biased the same way: an answer we
could not read degrades to the pre-existing, safer verdict rather than to a
confident new one.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "fault_of",
    "is_registered",
    "names_of",
    "rows_from_agents_body",
]


def rows_from_agents_body(body: Any) -> list[dict[str, Any]]:
    """Normalise a ``GET /agents`` body into a list of row dicts.

    Accepts both the current ``{"agents": [...]}`` envelope and the bare list
    older listens returned, and tolerates a list of bare NAME STRINGS by
    promoting each to ``{"name": ...}`` — so a modern caller keeps working
    against an old daemon instead of silently deciding the fleet is empty.

    Returns ``[]`` for anything unparseable. The caller must read that as "I
    could not determine", NEVER as "no agents exist".
    """
    rows = body.get("agents") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, str) and row:
            out.append({"name": row})
    return out


def names_of(rows: list[dict[str, Any]]) -> list[str]:
    """Every registered name in ``rows`` (``name``, falling back to ``agent``)."""
    names: list[str] = []
    for row in rows:
        name = row.get("name") or row.get("agent")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def is_registered(target: str, rows: list[dict[str, Any]]) -> bool:
    """True unless ``rows`` can AFFIRMATIVELY say ``target`` is absent.

    Deliberately biased toward the pre-existing behaviour. An empty list means
    the registry was unreadable (or genuinely empty), and "I could not check"
    is NOT evidence of a bad name — reporting an unknown target off a failed
    lookup would invent the exact false certainty the send-error module exists
    to prevent. So an unreadable registry says less, rather than something
    confident and possibly wrong.
    """
    names = names_of(rows)
    if not names:
        return True
    return target in names


def fault_of(target: str, rows: list[dict[str, Any]]) -> str | None:
    """The ``fault`` the listen route published for ``target``, or ``None``.

    ``None`` covers "no such row" AND "this listen does not publish the field"
    — an older daemon predating :mod:`.._listen._inbox_fault`. Both fall back
    to the undifferentiated no-subscriber verdict, which is what every caller
    got before this distinction existed and is therefore safe.
    """
    for row in rows:
        if (row.get("name") or row.get("agent")) == target:
            fault = row.get("fault")
            return fault if isinstance(fault, str) else None
    return None
