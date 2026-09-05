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

from scitex_agent_container._state import port_allocator as pa
from scitex_agent_container._state import port_allocator_store as pas


def test_port_store_reopens_after_its_connection_was_closed(pg_schema: str) -> None:
    # Arrange: a cached handle whose connection the peer (here: we) closed.
    pas._reset_store_cache()
    first = pas.port_store()
    first._connection.close()
    assert pas._handle_is_closed(first)

    # Act: the next caller asks the cache.
    second = pas.port_store()

    # Assert: a fresh, open handle — and the allocator works again on it.
    assert second is not first
    assert not pas._handle_is_closed(second)
    assert pa.get_port("nobody-claimed-this") is None


def test_port_store_keeps_an_open_cached_handle(pg_schema: str) -> None:
    # Arrange
    pas._reset_store_cache()
    first = pas.port_store()

    # Act
    second = pas.port_store()

    # Assert: the cache still saves the connect when nothing is wrong.
    assert second is first
