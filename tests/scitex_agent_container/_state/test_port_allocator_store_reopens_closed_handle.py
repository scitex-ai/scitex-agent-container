"""``port_store()`` must not hand back a cached handle whose connection is closed.

Measured 2026-09-05 on compute-03 and compute-04: the host tui-bridge-supervisor
logged "could not evaluate the bridge ... the connection is closed" every 30 s
for two hours, because its per-process cached claim-ledger handle had lost its
connection once and the cache never noticed. The fix is a local check on the
psycopg ``closed`` flag before the cached handle is returned.

Real PostgreSQL through the shared ``pg_schema`` fixture (skips without one);
the connection is closed for real, not faked.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state import port_allocator as pa
from scitex_agent_container._state import port_allocator_store as pas


@pytest.fixture
def closed_first_handle(pg_schema: str) -> object:
    """A cached handle whose connection the peer (here: we) closed for real."""
    pas._reset_store_cache()
    first = pas.port_store()
    first._connection.close()
    return first


def test_closed_flag_is_seen_on_the_cached_handle(closed_first_handle: object) -> None:
    # Arrange: fixture closed the connection.
    # Act
    closed = pas._handle_is_closed(closed_first_handle)
    # Assert
    assert closed


def test_port_store_returns_a_different_handle_after_a_close(
    closed_first_handle: object,
) -> None:
    # Arrange: fixture closed the connection.
    # Act
    second = pas.port_store()
    # Assert
    assert second is not closed_first_handle


def test_port_store_reopened_handle_is_open(closed_first_handle: object) -> None:
    # Arrange: fixture closed the connection.
    # Act
    second = pas.port_store()
    # Assert
    assert not pas._handle_is_closed(second)


def test_allocator_works_again_after_the_reopen(closed_first_handle: object) -> None:
    # Arrange: fixture closed the connection.
    # Act
    port = pa.get_port("nobody-claimed-this")
    # Assert
    assert port is None


def test_port_store_keeps_an_open_cached_handle(pg_schema: str) -> None:
    # Arrange
    pas._reset_store_cache()
    first = pas.port_store()
    # Act
    second = pas.port_store()
    # Assert: the cache still saves the connect when nothing is wrong.
    assert second is first
