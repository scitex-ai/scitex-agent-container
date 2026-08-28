"""Diary-style writes — on PostgreSQL only (sqlite→Postgres, 2026-08-28).

Three logical stores let every agent write a journal the lead reads:

  * ``turns``      — one row per state-transition of a /v1/turn flow
    (``queued``, ``delivered``, ``read``, ``responded``, ``error``).
    A successful turn produces four rows sharing a single ``turn_id``.
  * ``errors``     — one row per caught error (auth, network, sdk-crash,
    schema-mismatch, ...). Optionally tied to a ``turn_id``.
  * ``heartbeats`` — the per-agent ``heartbeat.json`` payload promoted
    into a cross-host queryable table; one row per tick.

WHY THIS MODULE MOVED
=====================
Operator ruling, restated 2026-08-28: 「スクライトなんて全部絶滅させて
ください」 and 「スクライト1個でも使ったら負け」 — because the fleet is
MULTI-HOST, and a SQLite file per host means a different truth per host.
That is not theoretical: the same evening, per-host agent SPECS were
measured diverging 122/137/122/124 across four machines. A per-host
state.db is the same failure in a different file format.

The move ADOPTS :mod:`scitex_dev.store`, the fleet's own store primitive,
rather than growing a private psycopg layer here — the same route
:mod:`.state_db_verdict_dedup` and :mod:`.state_db_incarnations` took.
That primitive's ``resolve_target`` is exactly two steps
(``SCITEX_STORE_DSN`` or the per-host PostgreSQL) with NO SQLite
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach. Fail fast, fail
loud, no fallbacks — implemented in the primitive, not re-argued here.

``db_path`` IS GONE from every signature. It named a SQLite file; there
is no file. Callers that threaded it through simply stop.

APPEND-ONLY IS PRESERVED, DELIBERATELY
======================================
The SQLite tables were append-only: every beat, turn and error was its
own row, and the history stayed queryable. The obvious key/value
shortcut — keying a heartbeat by agent NAME so a new beat overwrites the
last — would have been less code and would have SILENTLY DISCARDED that
history. Changing storage and semantics together is how a breaking
change rides along inside a migration unnoticed, so the identity of
every record here includes its timestamp: a new tick is a new record,
exactly as before.

TIMESTAMPS STAY REAL UNIX-SECONDS, the format the SQLite columns used
and the format callers already read. Same clock, same wire format,
different storage.

``expected_revision`` IS MANDATORY, AND ``NEW_RECORD`` IS THE RIGHT VALUE
========================================================================
``Store.put`` takes it keyword-only; omitting it is a ``TypeError`` at the
first write, not a review comment. Every record here is NEW by construction —
the identity carries the timestamp, so a new tick is a new key — which is
exactly what ``NEW_RECORD`` asserts.

``RevisionMismatchError`` therefore means one thing only: a record with this
identity already exists, i.e. a duplicate beat at a byte-identical timestamp.
"Already recorded" is the outcome we wanted, so it returns rather than raising.
The catch is deliberately NARROW — nothing else is swallowed, so an unreachable
store still fails loudly, which is the whole point of moving off SQLite.

THE ``lastrowid`` CONTRACT
==========================
``record_error`` returned SQLite's ``lastrowid`` and one caller
propagates it (``_runners/_session_beat.py``). PostgreSQL has no
``lastrowid``, so the id now comes from the store's own monotonic
``next_seq()``. That keeps the contract that matters — a unique,
increasing integer per record — without pretending to be a rowid.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: Logical store names. Each renders as four physical tables
#: (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
TURNS_STORE = "diary_turns"
ERRORS_STORE = "diary_errors"
HEARTBEATS_STORE = "diary_heartbeats"

_ACTOR = "scitex-agent-container"

# Bounds on the inline prompt/response and error-detail fields so a
# runaway message cannot bloat the store. Unchanged from the SQLite
# version — the operator's "first ~500 chars / first ~1000 chars" spec.
_TURN_TEXT_LIMIT = 500
_ERROR_DETAIL_LIMIT = 1000


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any) -> Any:
    """A recorded diary entry is a historical fact — IMMUTABLE.

    A merge that could move a ts or a state would silently rewrite the
    timeline the lead reads to decide whether an agent is alive.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=False,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _heartbeats_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=HEARTBEATS_STORE,
        fields={
            # ts is part of the IDENTITY on purpose: it is what keeps
            # this append-only. Same agent, same host, new tick -> new
            # record, not an overwrite.
            "name": _ident(FieldKind.TEXT),
            "host": _ident(FieldKind.TEXT),
            "ts": _ident(FieldKind.REAL),
            "pid": _fact(FieldKind.INTEGER),
            "state": _fact(FieldKind.TEXT),
        },
    )


def _errors_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=ERRORS_STORE,
        fields={
            "error_id": _ident(FieldKind.INTEGER),
            "name": _fact(FieldKind.TEXT),
            "host": _fact(FieldKind.TEXT),
            "cause": _fact(FieldKind.TEXT),
            "detail": _fact(FieldKind.TEXT),
            "ts": _fact(FieldKind.REAL),
            "turn_id": _fact(FieldKind.TEXT),
        },
    )


def _turns_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=TURNS_STORE,
        fields={
            # A turn produces FOUR rows sharing one turn_id, so turn_id
            # alone cannot identify a record — the status and the ts
            # complete it, preserving the append-only transition log.
            "turn_id": _ident(FieldKind.TEXT),
            "status": _ident(FieldKind.TEXT),
            "ts": _ident(FieldKind.REAL),
            "name": _fact(FieldKind.TEXT),
            "host": _fact(FieldKind.TEXT),
            "prompt_text": _fact(FieldKind.TEXT),
            "response_text": _fact(FieldKind.TEXT),
            "session_id": _fact(FieldKind.TEXT),
            "input_tokens": _fact(FieldKind.INTEGER),
            "output_tokens": _fact(FieldKind.INTEGER),
        },
    )


def _open(schema: Any) -> Store:
    """Open one diary store. RAISES if PostgreSQL is unreachable.

    Open-and-close per call mirrors the old ``with open_db(...)`` shape;
    the call rate is a heartbeat tick, not a request path.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def record_turn(
    *,
    turn_id: str,
    name: str,
    host: str,
    status: str,
    prompt_text: str | None = None,
    response_text: str | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    ts: float | None = None,
) -> None:
    """Append one ``turns`` record. Append-only, as before."""
    row_ts = float(ts) if ts is not None else time.time()
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    store = _open(_turns_schema())
    try:
        store.put(
            {
                "turn_id": turn_id,
                "status": status,
                "ts": row_ts,
                "name": name,
                "host": host,
                "prompt_text": _clip(prompt_text, _TURN_TEXT_LIMIT),
                "response_text": _clip(response_text, _TURN_TEXT_LIMIT),
                "session_id": session_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            expected_revision=NEW_RECORD,
        )
    except RevisionMismatchError:
        return  # same turn_id+status+ts already recorded
    finally:
        store.close()


def record_error(
    *,
    name: str,
    host: str,
    cause: str,
    detail: str | None = None,
    turn_id: str | None = None,
    ts: float | None = None,
) -> int:
    """Append one ``errors`` record. Returns the new ``error_id``.

    The id is the store's monotonic ``next_seq()`` rather than SQLite's
    ``lastrowid`` — see the module docstring.
    """
    from scitex_dev.store import NEW_RECORD

    row_ts = float(ts) if ts is not None else time.time()
    store = _open(_errors_schema())
    try:
        error_id = int(store.next_seq())
        store.put(
            {
                "error_id": error_id,
                "name": name,
                "host": host,
                "cause": cause,
                "detail": _clip(detail, _ERROR_DETAIL_LIMIT),
                "ts": row_ts,
                "turn_id": turn_id,
            },
            expected_revision=NEW_RECORD,
        )
        return error_id
    finally:
        store.close()


def record_heartbeat(
    *,
    name: str,
    host: str,
    pid: int | None,
    state: str,
    ts: float | None = None,
) -> int:
    """Append one ``heartbeats`` record. Returns its sequence number.

    ``state`` follows the runner's vocabulary (``starting`` | ``idle`` |
    ``working`` | ``stopping`` | ``error`` | ``down``).
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    row_ts = float(ts) if ts is not None else time.time()
    store = _open(_heartbeats_schema())
    try:
        seq = int(store.next_seq())
        store.put(
            {
                "name": name,
                "host": host,
                "ts": row_ts,
                "pid": pid,
                "state": state,
            },
            expected_revision=NEW_RECORD,
        )
        return seq
    except RevisionMismatchError:
        return seq  # a beat with this exact (name, host, ts) is already recorded
    finally:
        store.close()


def latest_heartbeats_per_name() -> list[dict]:
    """Return one heartbeat per agent ``name`` — the most recent.

    The SQLite version did this with a GROUP BY / MAX(ts) self-join. The
    store is row-oriented, so the reduction happens here instead. That is
    a deliberate trade: the fleet's beat table is tens of rows, and doing
    it in Python keeps this module free of a second query dialect.
    """
    store = _open(_heartbeats_schema())
    try:
        latest: dict[str, dict] = {}
        for row in store.rows():
            # ``row.values`` is the accessor, MEASURED not assumed. Row is a
            # dataclass whose ``key`` is a TUPLE of the identity values and
            # whose ``data`` is a bound METHOD; only ``values`` is the dict,
            # and it carries identity and data fields together. Two earlier
            # guesses here (``.fields``, then ``dict(row)``) both raised.
            if row.hidden:
                # Hidden is the store's soft-delete. A reader that ignores it
                # resurrects retired rows.
                continue
            data = dict(row.values)
            key = str(data.get("name", ""))
            prev = latest.get(key)
            if prev is None or float(data.get("ts") or 0) > float(
                prev.get("ts") or 0
            ):
                latest[key] = data
        return [latest[k] for k in sorted(latest)]
    finally:
        store.close()


__all__ = [
    "ERRORS_STORE",
    "HEARTBEATS_STORE",
    "TURNS_STORE",
    "latest_heartbeats_per_name",
    "record_error",
    "record_heartbeat",
    "record_turn",
]
