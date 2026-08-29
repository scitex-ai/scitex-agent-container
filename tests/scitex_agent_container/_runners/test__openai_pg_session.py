"""Tests for ``_runners/_openai_pg_session.py`` — the SDK ``Session`` on PostgreSQL.

Two tiers, matching the optional-dependency contract that
``test_openai_session.py`` states for the runner:

* **No store, no SDK** — construction, the ``session_id`` default, and the
  lazily-coerced ``session_settings``. These take NO ``pg_schema``, and the
  autouse unreachable-DSN guard is what makes that an assertion rather than
  an omission: if constructing a session connected, they would fail on port 1.
* **Real store** — ``pg_schema`` per test: the round trip, both REMOVAL verbs,
  the limit semantics, and the composite identity.

THE REMOVAL TESTS ARE THE POINT. ``items`` is ``LAST_WRITER_WINS`` rather than
``APPEND`` precisely because ``pop_item`` and ``clear_session`` take items
AWAY, and an append-merged collection has no representation for a gone
element. That reasoning is only worth what the tests prove, so both verbs are
exercised against a real store and read back.

PA-306: no mocks, no monkeypatch — isolation comes from the shared
``pg_schema`` fixture repointing ``SCITEX_STORE_DSN`` at a throwaway schema,
which exercises the REAL resolver. STX-TQ002 AAA + STX-TQ007
one-assert-per-test; async via ``asyncio.run(_go())``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scitex_agent_container._runners._openai_pg_session import PostgresAgentSession


def _run(coro_factory: Any) -> Any:
    """Drive one coroutine factory to completion.

    A helper rather than a fixture: every test below is a single ``asyncio``
    round trip, and threading an event loop through a fixture would put the
    store's own ``asyncio.to_thread`` calls on a loop the test does not own.
    """
    return asyncio.run(coro_factory())


# ---------------------------------------------------------------------------
# No store, no SDK — construction must not connect
# ---------------------------------------------------------------------------


def test_session_id_defaults_to_the_agent_name() -> None:
    # Arrange
    session = PostgresAgentSession("alpha")
    # Act
    resolved = session.session_id
    # Assert
    assert resolved == "alpha"


def test_an_explicit_session_id_wins() -> None:
    # Arrange
    session = PostgresAgentSession("alpha", session_id="review-thread")
    # Act
    resolved = session.session_id
    # Assert
    assert resolved == "review-thread"


def test_session_settings_is_none_when_unset() -> None:
    """``None`` in, ``None`` out — the SDK supplies its own default.

    Reading the property must ALSO not import ``agents``: this test runs on a
    Claude-only deployment, where it would raise.
    """
    # Arrange
    session = PostgresAgentSession("alpha")
    # Act
    settings = session.session_settings
    # Assert
    assert settings is None


def test_session_settings_coerces_a_mapping_to_the_sdk_dataclass() -> None:
    # Arrange
    pytest.importorskip("agents")
    from agents.memory.session_settings import SessionSettings

    session = PostgresAgentSession("alpha", session_settings={"limit": 3})
    # Act
    settings = session.session_settings
    # Assert
    assert isinstance(settings, SessionSettings)


def test_session_settings_keeps_the_coerced_limit() -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha", session_settings={"limit": 3})
    # Act
    limit = session.session_settings.limit
    # Assert
    assert limit == 3


def test_it_satisfies_the_sdk_session_protocol() -> None:
    """Structural conformance, checked against the SDK's own Protocol.

    ``PostgresAgentSession`` deliberately does not subclass ``SessionABC``
    (which the SDK documents as internal), so nothing but this check stands
    between a renamed method and a session the ``Runner`` silently ignores.
    """
    # Arrange
    pytest.importorskip("agents")
    from agents.memory.session import Session

    session = PostgresAgentSession("alpha")
    # Act
    conforms = isinstance(session, Session)
    # Assert
    assert conforms is True


# ---------------------------------------------------------------------------
# Real store — every test below takes pg_schema because every one does I/O
# ---------------------------------------------------------------------------


def test_get_items_on_an_unknown_session_is_empty(pg_schema: str) -> None:
    """The Protocol has no "no such session" answer; absent reads as empty."""
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")
    # Act
    items = _run(lambda: session.get_items())
    # Assert
    assert items == []


def test_added_items_come_back_in_order(pg_schema: str) -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")
    written = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]

    async def _go() -> list[Any]:
        await session.add_items(list(written))
        return await session.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == written


def test_add_items_appends_rather_than_replacing(pg_schema: str) -> None:
    """Two writes accumulate. LAST_WRITER_WINS is on the FIELD, not the verb."""
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items([{"role": "user", "content": "one"}])
        await session.add_items([{"role": "user", "content": "two"}])
        return await session.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert [item["content"] for item in items] == ["one", "two"]


def test_pop_item_returns_the_newest_item(pg_schema: str) -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> Any:
        await session.add_items(
            [{"role": "user", "content": "old"}, {"role": "user", "content": "new"}]
        )
        return await session.pop_item()

    # Act
    popped = _run(_go)
    # Assert
    assert popped == {"role": "user", "content": "new"}


def test_pop_item_actually_removes_it(pg_schema: str) -> None:
    """The REMOVAL half, read back from the store.

    This is the assertion an ``APPEND``-merged ``items`` field could not
    satisfy: a merge rule that only grows a collection would hand the popped
    item straight back on the next read.
    """
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items(
            [{"role": "user", "content": "old"}, {"role": "user", "content": "new"}]
        )
        await session.pop_item()
        return await session.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"role": "user", "content": "old"}]


def test_pop_item_on_an_empty_session_is_none(pg_schema: str) -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")
    # Act
    popped = _run(lambda: session.pop_item())
    # Assert
    assert popped is None


def test_clear_session_empties_the_conversation(pg_schema: str) -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items([{"role": "user", "content": "forget me"}])
        await session.clear_session()
        return await session.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == []


def test_get_items_limit_returns_the_latest_n(pg_schema: str) -> None:
    """A positive limit returns the LAST n, still oldest-first."""
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items([{"n": index} for index in range(5)])
        return await session.get_items(limit=2)

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"n": 3}, {"n": 4}]


def test_get_items_limit_zero_returns_nothing(pg_schema: str) -> None:
    """``0`` means none and a NEGATIVE limit means all — SQLite's own
    ``LIMIT`` semantics, kept deliberately so existing callers keep their
    answer rather than being tidied into a single "no limit" branch."""
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items([{"n": 1}])
        return await session.get_items(limit=0)

    # Act
    items = _run(_go)
    # Assert
    assert items == []


def test_get_items_negative_limit_returns_everything(pg_schema: str) -> None:
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await session.add_items([{"n": 1}, {"n": 2}])
        return await session.get_items(limit=-1)

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"n": 1}, {"n": 2}]


def test_session_settings_limit_applies_without_an_explicit_one(
    pg_schema: str,
) -> None:
    """``resolve_session_limit`` precedence, exercised end to end."""
    # Arrange
    pytest.importorskip("agents")
    session = PostgresAgentSession("alpha", session_settings={"limit": 1})

    async def _go() -> list[Any]:
        await session.add_items([{"n": 1}, {"n": 2}])
        return await session.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"n": 2}]


def test_two_agents_sharing_a_session_id_keep_separate_histories(
    pg_schema: str,
) -> None:
    """The identity is COMPOSITE, and this is why.

    ``session_id`` is the caller's own choice, so two agents picking the same
    one is a naming accident rather than anything a reviewer would spot. Keyed
    on ``session_id`` alone they would read each other's conversation.
    """
    # Arrange
    pytest.importorskip("agents")
    alpha = PostgresAgentSession("alpha", session_id="shared")
    beta = PostgresAgentSession("beta", session_id="shared")

    async def _go() -> list[Any]:
        await alpha.add_items([{"who": "alpha"}])
        await beta.add_items([{"who": "beta"}])
        return await alpha.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"who": "alpha"}]


def test_close_leaves_the_shared_handle_usable(pg_schema: str) -> None:
    """``close()`` is a documented NO-OP, and this is the property behind it.

    The runner calls ``close()`` on whatever session object it holds. The
    handle is the process-wide cached ``Store``, so closing it would break
    every other session in the process — asserted by reading through a SECOND
    session object after the first was closed.
    """
    # Arrange
    pytest.importorskip("agents")
    writer = PostgresAgentSession("alpha")
    reader = PostgresAgentSession("alpha")

    async def _go() -> list[Any]:
        await writer.add_items([{"role": "user", "content": "still here"}])
        writer.close()
        return await reader.get_items()

    # Act
    items = _run(_go)
    # Assert
    assert items == [{"role": "user", "content": "still here"}]

# EOF
