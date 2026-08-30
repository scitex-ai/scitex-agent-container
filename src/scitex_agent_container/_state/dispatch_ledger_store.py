"""Store plumbing for the dispatch ledger — schema, target, ordering.

Extracted from :mod:`.dispatch_ledger` so that module stays under the per-file
line cap, exactly as :mod:`.state_db_grants` was extracted from
:mod:`.state_db_nodes`. Everything public here is re-exported from
``dispatch_ledger``, so the existing import surface is unchanged.

This file holds the two decisions the port turned on — WHAT the identity is,
and WHAT ORDER rows come back in — and the reasoning for each. The verbs that
use them live next door.

WHOSE LEDGER IS THIS: THE DEFECT THAT HAD TO BE FIXED BEFORE THE MOVE
=====================================================================
The two backends have OPPOSITE shapes and a naive port silently joins them:

  * ``state.db`` was PER-AGENT. Every agent had its own database file, so
    ``list_dispatches()`` with no filters meant "my dispatches" — the SHARD
    did the scoping and no code had to.
  * ``SCITEX_STORE_DSN`` is FLEET-WIDE. ``runtimes/_fleet_env`` injects ONE
    value into every container and ``scitex_dev.store`` resolves it first, so
    130+ per-agent shards collapse into ONE ``dispatches_rows`` table.

Ported as-is, ``list_dispatches()`` would hand the WHOLE FLEET's outbound
traffic to whoever asked, and ``list_unreacted_dispatches()`` would report
every other agent's comm-misses as this agent's own. NOTHING WOULD RAISE — no
column missing, no query failing, no store unreachable. The answers just
quietly become wrong, in the direction of "there is much more traffic than I
sent", which reads as data rather than as a bug.

``from_agent`` looks like the owner and is not. It names the SENDER of one
message, and it is explicitly NULLABLE — a script driving ``post_turn``
outside an agent container has no ``SAC_NAME``, which
``test_record_dispatch_allows_null_agents`` has pinned as legal since 2026-05.
A NULL owner is unfilterable by construction: in a per-agent shard that row is
still findable because the FILE named its owner; in one shared table it
belongs to nobody and is returned to everybody.

So the schema below adds ``agent`` — the OWNING agent, the process whose
ledger the row is — in the store IDENTITY, exactly where
:mod:`.inbound_ledger` put its own ``agent`` one table earlier. Identity is
``(agent, dispatch_id)``.

``agent=""`` IS A REAL VALUE, NOT A MISSING ONE. Store IDENTITY fields must be
present and the ops-script case has no owner to name, so it records ``""`` —
"this dispatch belongs to no agent". That row is returned by an UNFILTERED
``list_dispatches()`` and by NO scoped one, which is right both ways: it is
nobody's, so it leaks to nobody. The unfiltered read stays fleet-wide
deliberately — ``list_inbound(agent=None)`` in the mirror module behaves the
same way, and the fix is not to make an unfiltered read lie but to give a
caller a way to ask for their own.

THE COST OF EVERY READ IS O(n), STATED RATHER THAN HIDDEN
=========================================================
``Store`` exposes ``get``/``put``/``rows``, not SQL — no WHERE clause and no
index to lean on — so every listing materialises the whole ledger and filters
in Python where the old module pushed four indexes at the database. That is a REAL
regression, and this is the worst table for it so far: it grows by one row per
a2a message per agent, fleet-wide, where ``verdict_delivered`` grew by a
handful a day. Two things keep it acceptable and neither is permanent — the
ledger starts EMPTY (nothing is migrated in from the old shards) and both list
functions are observability surfaces with zero production callers. The WRITE
path stays O(1): a keyed ``get``/``put``, never a scan. If
``list_unreacted_dispatches`` ever becomes a polled dashboard it wants an
indexed query — recorded so a future reader finds a decision, not a surprise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "dispatches"

#: Every write is attributed to one actor in the oplog.
ACTOR = "scitex-agent-container"

#: The identity fields, in order. ``agent`` FIRST because it is the scope: a
#: reader asks "mine?" before it asks "which one?".
IDENTITY_FIELDS = ("agent", "dispatch_id")

__all__ = [
    "ACTOR",
    "IDENTITY_FIELDS",
    "STORE_NAME",
    "dispatch_store_target",
    "open_dispatch_store",
    "sorted_values",
]


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any, *, required: bool = False) -> Any:
    """A field describing a send that already happened — IMMUTABLE.

    No merge may rewrite who sent what to whom, or when. ``ts`` in particular
    ORDERS the ledger; a rule that could move it would silently reorder
    history.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _moving(kind: Any) -> Any:
    """``status`` is the ONLY field that moves: sent -> delivered/reacted/…"""
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=True,
        merge=MergeRule.LAST_WRITER_WINS,
        indexed=False,
    )


def _schema() -> Any:
    """The dispatch-ledger schema.

    Built lazily so importing this module does not import scitex-dev; the old
    module was equally lazy about ``state_db``, for the same reason.
    """
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=STORE_NAME,
        fields={
            "agent": _ident(FieldKind.TEXT),
            "dispatch_id": _ident(FieldKind.TEXT),
            "from_agent": _fact(FieldKind.TEXT),
            "to_agent": _fact(FieldKind.TEXT),
            "conversation_id": _fact(FieldKind.TEXT),
            "text_summary": _fact(FieldKind.TEXT),
            "status": _moving(FieldKind.TEXT),
            "ts": _fact(FieldKind.REAL, required=True),
        },
    )


def dispatch_store_target() -> Any:
    """Resolve WHERE the dispatch ledger lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_dispatch_store() -> Store:
    """Open the ledger. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public verb opens and closes one per
    call, mirroring the old ``with open_db(...)`` shape.

    MULTI_WRITER, and this is where the fleet-wide DSN changes the honest
    answer rather than merely the scale. Under a per-agent file exactly
    one process ever wrote a row; under one shared table every agent on every
    host writes into it, so the ownership check SINGLE_WRITER runs would refuse
    legitimate writes from the second host onward.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        dispatch_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


def _row_sort_key(row: Row) -> tuple:
    """Ledger order, made TOTAL. :func:`sorted_values` reverses it.

    ``ts`` preserves the old ``ORDER BY ts DESC``. The hybrid logical clock
    breaks ties, because two agents on two hosts can mint the same float
    instant and wall-clock alone would order them by whichever machine's clock
    ran fast; ``node`` is the final tiebreak, since two origins can produce the
    same (wall_us, logical) pair.
    """
    hlc = row.hlc
    return (float(row.values.get("ts") or 0.0), hlc.wall_us, hlc.logical, hlc.node)


def sorted_values(rows: list[Row]) -> list[dict[str, Any]]:
    """Row values as plain dicts, newest first.

    ``Store.rows()`` is NOT ordered, so this is not decoration: without it the
    "newest first" every caller's docstring promises would be whatever order
    PostgreSQL happened to return.
    """
    return [dict(row.values) for row in sorted(rows, key=_row_sort_key, reverse=True)]
