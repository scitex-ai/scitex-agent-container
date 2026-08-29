"""``PostgresAgentSession`` — the ``openai-agents`` ``Session`` on PostgreSQL.

The replacement for the SDK's own file-backed session object, and the last
piece of sac's SQLite eradication: the runner's conversation memory was the
only database left that no ``import sqlite3`` scan could see, because the
import happened inside the vendor package rather than here.

WHAT THE PROTOCOL ACTUALLY IS (read from the installed SDK, not remembered)
==========================================================================
``agents.memory.session.Session`` is a ``@runtime_checkable`` Protocol with two
attributes and four coroutines, measured against openai-agents 0.22.0::

    session_id: str
    session_settings: SessionSettings | None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]
    async def add_items(self, items: list[TResponseInputItem]) -> None
    async def pop_item(self) -> TResponseInputItem | None
    async def clear_session(self) -> None

Three details of the SDK's own call sites are load-bearing and are NOT
guessable from the signatures:

* ``run_internal/session_persistence.py`` reads settings as
  ``getattr(session, "session_settings", None) or SessionSettings()`` and then
  calls ``.resolve(...)`` on the result — so the attribute must be a real
  ``SessionSettings``, not a dict, or the run raises on an ordinary turn.
* ``memory/session.py::_session_accepts_wrapper`` INSPECTS the signatures of
  all four methods and passes a ``wrapper=`` keyword only when EVERY one of
  them accepts it. Keeping the released signatures keeps us on the legacy
  shape, which is what the Protocol documents for third-party sessions.
* ``resolve_session_limit(limit, settings)`` is the SDK's own precedence rule
  (explicit limit wins, else the settings' limit, else unlimited). Re-deriving
  it here would be a second copy that drifts, so it is imported.

Everything from ``agents`` is imported INSIDE the methods. The SDK is an
OPTIONAL extra (``pip install scitex-agent-container[openai]``) and importing
this module must stay side-effect-free on Claude-only deployments — the same
contract :mod:`.openai_session` holds.

WHY THE REVISION IS READ BEFORE THE ROW
=======================================
Every mutation is a read-modify-write, because ``items`` is one
LAST_WRITER_WINS JSON list (see :mod:`.._state.openai_session_store` for why
APPEND cannot express ``pop_item`` / ``clear_session``). The ORDER of the two
reads decides whether a concurrent turn is caught or silently swallowed:

* ``revision()`` then ``get()`` — a writer landing between them leaves us
  holding NEWER data with an OLDER revision token, so our ``put`` is REFUSED
  with ``RevisionMismatchError`` and we retry. Safe.
* ``get()`` then ``revision()`` — a writer landing between them leaves us
  holding STALE data with a revision token that already covers the other
  writer's change, so our ``put`` SUCCEEDS and silently discards their turn.

The second order fails in the direction that loses a conversation without
raising, which is why the first is written here and stated in prose: it looks
like an arbitrary swap and is not. This is the same ordering
``port_allocator_store.try_claim`` depends on for its takeover.

FIVE ATTEMPTS, THEN RAISE
=========================
A retry loop bounded at :data:`_MAX_ATTEMPTS`. Losing a race is normal and
retrying is correct; losing it five times in a row is not a race any more, and
looping forever would turn a stuck session into a hung turn with no log line.
The last ``RevisionMismatchError`` is re-raised with its own context, so the
failure names the record rather than arriving as a timeout somewhere else.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .._state.openai_session_store import (
    items_of,
    session_key,
    session_store,
    session_values,
)

__all__ = ["PostgresAgentSession", "PostgresSessionConflictError"]

#: Read-modify-write attempts before a concurrent-writer conflict is fatal.
_MAX_ATTEMPTS = 5


class PostgresSessionConflictError(RuntimeError):
    """A conversation write lost its optimistic lock ``_MAX_ATTEMPTS`` times."""


class PostgresAgentSession:
    """``openai-agents`` conversation memory backed by the fleet's PostgreSQL.

    Structurally satisfies the SDK's ``Session`` Protocol; it deliberately
    does NOT subclass ``SessionABC``, which the SDK documents as internal and
    which would make ``agents`` a hard import of this module.

    Args:
        agent_name: The sac agent that owns the conversation. Half of the
            store identity — ``session_id`` alone is the CALLER's choice and
            two agents picking the same one is a naming accident, not a bug
            anyone would notice.
        session_id: Logical conversation key. Defaults to ``agent_name``,
            matching what :class:`~.openai_session.OpenAIAgentsSession` passed
            to the SDK's own session object.
        session_settings: ``SessionSettings`` or a plain mapping; coerced
            LAZILY on first read (see :attr:`session_settings`) so
            constructing a session never imports the optional SDK.
    """

    def __init__(
        self,
        agent_name: str,
        *,
        session_id: str | None = None,
        session_settings: Any = None,
    ) -> None:
        self.agent_name = agent_name
        self.session_id = session_id or agent_name
        self._raw_settings = session_settings
        self._settings: Any = None
        self._settings_coerced = False

    # -- Session protocol surface ----------------------------------------

    @property
    def session_settings(self) -> Any:
        """The SDK's ``SessionSettings``, coerced on FIRST READ and cached.

        A property rather than a plain attribute because the coercion needs
        ``agents``, and this class must be constructible on a deployment that
        has never installed it. The SDK reads this attribute during a run
        (``session_persistence.prepare_input_with_session``), by which point
        the SDK is necessarily importable.

        ``None`` in, ``None`` out — the SDK's own reader is
        ``getattr(session, "session_settings", None) or SessionSettings()``,
        so it supplies the default itself and inventing one here would only
        add a second place for it to change.
        """
        if not self._settings_coerced:
            if self._raw_settings is None:
                self._settings = None
            else:
                from agents.memory.session_settings import coerce_session_settings

                self._settings = coerce_session_settings(self._raw_settings)
            self._settings_coerced = True
        return self._settings

    async def get_items(self, limit: int | None = None) -> list[Any]:
        """The conversation history, oldest first.

        ``limit`` follows the SDK's own precedence via
        ``resolve_session_limit``: an explicit value wins, else the session
        settings' limit, else everything.

        The three limit cases are the file-backed session's, kept
        deliberately rather than tidied. ``> 0`` returns the LATEST N items in
        chronological order — the documented contract. ``0`` returns NOTHING
        and a NEGATIVE value returns EVERYTHING, which reads like a bug and is
        SQLite's own ``LIMIT`` semantics that the previous implementation
        explicitly preserved; collapsing them into one "no limit" branch would
        change what ``get_items(0)`` answers for every existing caller.

        An unknown session answers ``[]``. The Protocol has no
        "no such session" outcome — see ``items_of``.
        """
        from agents.memory.session_settings import resolve_session_limit

        session_limit = resolve_session_limit(limit, self.session_settings)
        items = await asyncio.to_thread(self._read_items)
        if session_limit is None or session_limit < 0:
            return items
        if session_limit == 0:
            return []
        return items[-session_limit:]

    async def add_items(self, items: list[Any]) -> None:
        """Append ``items`` to the conversation.

        An empty list is a no-op that does NOT touch the store: the SDK calls
        this after every turn, including turns that produced nothing, and a
        write per no-op would bump ``updated_at`` and burn a revision for a
        conversation that did not change.
        """
        if not items:
            return
        new_items = list(items)

        def _append(current: list[Any]) -> tuple[list[Any], None]:
            return [*current, *new_items], None

        await asyncio.to_thread(self._mutate, _append)

    async def pop_item(self) -> Any:
        """Remove and return the newest item, or ``None`` when empty.

        A REMOVAL, and the reason ``items`` cannot be an APPEND-merged
        collection: the store's merge rules can grow a list and cannot shrink
        one, so this operation has no representation under APPEND at all.
        """

        def _pop(current: list[Any]) -> tuple[list[Any], Any]:
            if not current:
                return current, None
            return current[:-1], current[-1]

        return await asyncio.to_thread(self._mutate, _pop)

    async def clear_session(self) -> None:
        """Drop every item, keeping the record.

        The record survives as an empty conversation rather than being hidden.
        ``Store.hide`` is the only removal the store offers and a hidden
        record still OCCUPIES its identity, so hiding here would make the next
        ``add_items`` re-take a tombstone — the trap
        ``port_allocator_store`` documents at length. An empty list says the
        same thing with none of that.
        """

        def _clear(current: list[Any]) -> tuple[list[Any], None]:
            return [], None

        await asyncio.to_thread(self._mutate, _clear)

    def close(self) -> None:
        """Release nothing, on purpose — and say so where a reader will look.

        :meth:`~.openai_session.OpenAIAgentsSession.close` calls ``close()``
        on whatever session object it holds, because the SDK's file-backed one
        owned connections that had to be shut. This one holds NO resource of
        its own: the handle is the per-process cached ``Store``
        (``_state.openai_session_store.session_store``), shared by every
        session in the process, and closing it here would break the others and
        pay a fresh psycopg connect on the next turn.
        """

    # -- internals -------------------------------------------------------

    def _read_items(self) -> list[Any]:
        """The stored conversation, as a plain list. Blocking; runs in a thread."""
        store = session_store()
        return items_of(store.get(session_key(self.agent_name, self.session_id)))

    def _mutate(self, change: Callable[[list[Any]], "tuple[list[Any], Any]"]) -> Any:
        """Read-modify-write ``items`` under an optimistic lock.

        ``change`` receives the current list and returns ``(new_list,
        result)``; ``result`` is handed back to the caller (``pop_item`` needs
        the removed item, the others need nothing). It must be PURE — it is
        re-run on every retry.

        The revision is read BEFORE the row; the module docstring carries the
        measured reason that order is not interchangeable.

        A change that changes NOTHING writes nothing. That is not an
        optimisation: ``pop_item`` on an unknown session and
        ``clear_session`` on one would otherwise CREATE an empty record, and
        the file-backed session they replace ran a ``DELETE`` that matched no
        rows and left the database untouched. Writing here would invent a
        conversation out of an operation whose whole meaning is removal.
        """
        from scitex_dev.store import NEW_RECORD, RevisionMismatchError

        store = session_store()
        key = session_key(self.agent_name, self.session_id)
        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            revision = store.revision(key)
            current = items_of(store.get(key))
            new_items, result = change(current)
            if new_items == current:
                return result
            expected = NEW_RECORD if revision is None else revision
            try:
                store.put(
                    session_values(
                        self.agent_name, self.session_id, new_items, time.time()
                    ),
                    expected_revision=expected,
                )
            except RevisionMismatchError as exc:
                last_error = exc
                continue
            return result
        raise PostgresSessionConflictError(
            f"conversation {self.agent_name}/{self.session_id} lost its "
            f"optimistic lock {_MAX_ATTEMPTS} times running; another writer is "
            f"holding the session. Last store error: {last_error}"
        ) from last_error

# EOF
