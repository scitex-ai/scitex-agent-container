"""``openai_sessions`` — the OpenAI runner's conversation state, on PostgreSQL.

The storage adapter behind :mod:`.._runners._openai_pg_session`, split by the
same RESPONSIBILITY line as :mod:`.port_allocator_store`: this file knows how a
conversation is STORED, and the runner module knows the ``openai-agents``
``Session`` protocol it has to satisfy. Nothing here imports ``agents``, so a
Claude-only deployment can import it without the optional SDK.

WHY THIS EXISTS AT ALL
======================
The ``openai-agents`` SDK persists multi-turn memory through a ``Session``
object, and sac used the SDK's own file-backed one — a real per-agent
database under ``~/.scitex/agent-container/runtime/openai-sessions/``. It was
the LAST local database sac opened, and the only one no import scan could
ever see, because the import happens inside the vendor package. That is the
whole hole the vendor gate in
``tests/develop/test_retired_engine_footprint_frozen.py`` was written to
measure, and closing it is what took that gate's live population to zero.

THE RUNNER IS LIVE, so this is not a paper migration: handyman-05's spec sets
``handler: openai_session``, eleven specs name ``openai-agents``, and the
handymen drive local Qwen models through an OpenAI-compatible gateway. That
directory held ZERO database files anywhere on the fleet when this was
written, and that is NOT evidence of disuse — the SDK created the file lazily,
on the first session write.

``items`` IS LAST_WRITER_WINS, NOT APPEND — AND THAT IS THE LOAD-BEARING CHOICE
==============================================================================
A conversation reads like an append-only log, and ``MergeRule.APPEND`` exists,
so the natural-looking schema is the wrong one. The ``Session`` protocol has
FOUR operations and two of them are REMOVALS: ``pop_item`` deletes the most
recent item and returns it (the SDK's own retry/edit path), and
``clear_session`` deletes every item. ``_merge.merge_field`` under APPEND only
ever grows a collection — it has no representation for "this element is gone" —
so a popped item would reappear at the next merge and a cleared session would
come back in full. Under APPEND the two removal verbs are not merely
lossy, they are IMPOSSIBLE.

LAST_WRITER_WINS makes the whole item list the unit of change, which is exactly
the semantics the protocol asks for: every mutation is a read-modify-write of
the list, and the newest complete list is the conversation. The cost is that
two concurrent turns on ONE session id race — and that race is caught rather
than merged, because every write in :mod:`.._runners._openai_pg_session` passes
the revision it read as ``expected_revision``, so the loser gets a
``RevisionMismatchError`` and retries against the winner's list.

WriterPolicy.MULTI_WRITER
=========================
``Store`` is constructed with ``node=socket.gethostname()`` and sac RELOCATES
agents between hosts (``_state/relocation_pg.py`` exists for precisely that).
Under SINGLE_WRITER the first host to write a session would own it forever, and
the ordinary act of moving an agent to another machine would turn its next turn
into an illegal write. The same reasoning ``port_allocator_store`` and
``state_db_grants`` give for their own stores applies here, one level up: the
record has no single stable owning node.

THE STORE HANDLE IS CACHED PER PROCESS
======================================
``Store.__init__`` pays a psycopg connect (measured 10.7 ms) plus a schema
advisory lock and two catalogue probes on EVERY construction, and this store is
touched on the hot path — the SDK reads the history before a turn and writes it
back after, so a per-call handle would pay that twice per turn. So the module
holds ONE Store per (resolved target, pid) behind a lock, exactly as
:func:`.port_allocator_store.port_store` does.

The cache key is the ``StoreTarget`` VALUE, not ``str(locator)``: the locator's
string form is a redacted description that DROPS the DSN query string, so two
``pg_schema`` DSNs differing only in ``?options=-csearch_path`` stringify
identically and a string-keyed cache would hand the second test the first
test's schema. The pid is in the key so a forked child never reuses — or
closes — the parent's connection through an inherited fd.
:func:`_reset_store_cache` is the explicit reset for tests; no monkeypatch.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "openai_sessions"

#: Recorded on every write as the acting component.
ACTOR = "scitex-agent-container"

__all__ = [
    "ACTOR",
    "STORE_NAME",
    "init_openai_session_schema",
    "items_of",
    "open_session_store",
    "session_key",
    "session_store",
    "session_store_target",
    "session_values",
]


def _schema() -> Any:
    """The conversation-state schema.

    Built lazily so importing this module does not import scitex-dev; the
    The original code was equally lazy about its own imports, for the same
    reason (import cost off the hot path).

    ``(agent, session_id)`` is the composite IDENTITY, and the store requires
    IMMUTABLE on identities. Both halves are needed: ``session_id`` alone
    would collide the moment two agents pick the same logical conversation
    key, and the SDK's default session id IS the caller's own choice, so a
    collision is a naming accident rather than a bug anyone would notice.

    ``items`` carries the whole conversation as a JSON array under
    LAST_WRITER_WINS — the module docstring carries the measured reason APPEND
    is wrong here (it cannot express ``pop_item`` or ``clear_session``).

    ``updated_at`` is epoch REAL, not ISO text. Every migrated timestamp
    column across ``_state`` is REAL, and this one exists so an operator can
    ask which sessions are stale without decoding the item list.

    THIS IS THE FIRST ``FieldKind.JSON`` FIELD IN sac, so the binding was
    measured rather than assumed. ``_codec.encode`` renders a JSON field with
    ``json.dumps``, i.e. hands psycopg a ``str``, and the Postgres dialect
    declares the column ``JSONB`` — for which ``pg_cast`` holds NO cast from
    ``text``. That looks like a guaranteed
    ``column "items" is of type jsonb but expression is of type text``. It is
    not: psycopg3 sends ``str`` parameters as UNKNOWN, not as ``text``, so the
    server coerces them to the target column's type. Verified against a live
    PostgreSQL 16 on 2026-08-29 — ``SELECT jsonb_typeof(%s)`` with a Python
    ``str`` answers ``'object'``, and ``SELECT pg_typeof(%s)`` raises
    ``IndeterminateDatatype``, which is the untyped parameter saying so
    itself. (``StrDumper.oid`` reads 25 in the psycopg source; that class
    attribute is not what goes on the wire for this path, which is why the
    probe and not the source settled it.)
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def data(kind: Any, *, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=True,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "agent": ident(FieldKind.TEXT),
            "session_id": ident(FieldKind.TEXT),
            "items": data(FieldKind.JSON),
            "updated_at": data(FieldKind.REAL),
        },
    )


def session_store_target() -> Any:
    """Resolve WHERE conversation state lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_session_store() -> "Store":
    """Open a FRESH conversation-state handle. RAISES if PostgreSQL is down.

    The caller owns closing it. This is the constructor for callers that want
    a handle of their own; the runner goes through the per-process cache in
    :func:`session_store` instead, so a turn does not pay the connect twice.

    MULTI_WRITER — see the module docstring: an agent relocated to another
    host must be able to continue its own conversation, and ``node`` is the
    hostname.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        session_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


#: ``(StoreTarget, pid, Store)`` — see the module docstring's cache section.
#: Guarded by ``_STORE_LOCK``; reset with :func:`_reset_store_cache`.
_STORE_CACHE: "tuple[Any, int, Store] | None" = None
_STORE_LOCK = threading.Lock()


def session_store() -> "Store":
    """The per-process cached conversation store. Do NOT close the result.

    Keyed by the RESOLVED target and the pid (the module docstring says why:
    a psycopg connect per call on the turn path, while the ``pg_schema``
    fixture and forked test processes both invalidate a naive singleton).
    ``Store`` serialises its own operations internally, so one shared handle
    per process is safe for concurrent threads — which matters here because
    the async session runs every store call through ``asyncio.to_thread``.
    """
    global _STORE_CACHE
    import os

    target = session_store_target()
    pid = os.getpid()
    with _STORE_LOCK:
        if _STORE_CACHE is not None:
            cached_key, cached_pid, cached = _STORE_CACHE
            if cached_key == target and cached_pid == pid:
                return cached
            # A fork inherited the parent's connection through the same fd:
            # closing it HERE would send a termination on the parent's socket.
            # Only the process that opened a handle may close it; a
            # stale-target handle in the same process is closed so the fd does
            # not leak per test.
            if cached_pid == pid:
                cached.close()
        fresh = open_session_store()
        _STORE_CACHE = (target, pid, fresh)
        return fresh


def _reset_store_cache() -> None:
    """Drop (and close) the cached handle. For tests — plain call, no patching."""
    global _STORE_CACHE
    import os

    with _STORE_LOCK:
        if _STORE_CACHE is not None and _STORE_CACHE[1] == os.getpid():
            _STORE_CACHE[2].close()
        _STORE_CACHE = None


def init_openai_session_schema() -> str:
    """Create the conversation tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — it names WHERE the
    conversations actually went, so an operator can check rather than assume.
    """
    session_store()  # Store.__init__ creates the tables when absent.
    return str(session_store_target().locator)


def session_key(agent: str, session_id: str) -> dict[str, Any]:
    """The IDENTITY of one conversation. One place, so the shape cannot drift."""
    return {"agent": str(agent), "session_id": str(session_id)}


def session_values(
    agent: str, session_id: str, items: list[Any], updated_at: float
) -> dict[str, Any]:
    """The full record a conversation write lands. Identity + the whole list.

    The list is passed WHOLE rather than as a delta because ``items`` is
    LAST_WRITER_WINS: the caller has already done the read-modify-write, and
    what it hands over is the conversation as it should now stand.
    """
    return {
        **session_key(agent, session_id),
        "items": list(items),
        "updated_at": float(updated_at),
    }


def items_of(row: "Row | None") -> list[Any]:
    """The conversation items on ``row``, or ``[]`` when there is no record.

    An ABSENT record and an EMPTY conversation are deliberately collapsed
    here, and only here: the ``Session`` protocol has no "no such session"
    answer — ``get_items`` on an unknown id returns an empty list — so the
    distinction has to disappear somewhere, and the honest place is the one
    function whose whole job is decoding a row.

    ``Row.values`` is a mapping (not iterable, and not ``.fields``). The JSON
    field decodes to whatever was stored; a non-list value is coerced to ``[]``
    rather than propagated, because the caller's next act is to index it.
    """
    if row is None:
        return []
    items = row.values.get("items")
    return list(items) if isinstance(items, list) else []

# EOF
