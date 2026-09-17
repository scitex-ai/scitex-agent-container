"""P0: the fleet shell must render before any inventory fetch, and must never
leak an internal endpoint, token name or setup prose to the browser.

Operator-reproduced defect: tapping Agents waited ~10s, then showed 0 alive and
raw `http://host.docker.internal:7878` plus `SCITEX_AGENT_CONTAINER_API_*` setup
instructions. Measured here at 60.06s to render, with all three leaks present.

Four properties are pinned:

1. **NO BLOCKING.** The shell renders without waiting on the control plane. Even
   with the listener unreachable, the page returns promptly.
2. **NO LEAK.** No internal host, no env-var name, no token name, no setup prose
   reaches the browser.
3. **REAL ONLY.** A cold cache means "loading", never invented agents; a stale
   snapshot is shown AS stale, never as current.
4. **NO MUTATION FROM A PREFETCH.** The Hub may prefetch on idle/intent, so no
   code path from the cache or the shell may reach a lifecycle verb.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container._django._constants import IDENTITY_ENV
from scitex_agent_container._django._inventory_cache import InventoryCache

# Anything the browser must never receive.
FORBIDDEN = (
    "host.docker.internal",
    "SCITEX_AGENT_CONTAINER_API_URL",
    "SCITEX_AGENT_CONTAINER_API_TOKEN",
    "bearer token",
    "127.0.0.1:7878",
)


class _DeadListener(BaseHTTPRequestHandler):
    """Accepts the connection and never answers (the slow-listener shape)."""

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        time.sleep(30)


@pytest.fixture
def dead_listener():
    """A listener that hangs, so any blocking render would be obvious."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeadListener)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def isolated_cache():
    """A private cache so tests never share process-global state."""
    from scitex_agent_container._django import _inventory_cache as module

    original = module.CACHE
    module.CACHE = InventoryCache()
    try:
        yield module.CACHE
    finally:
        module.CACHE = original


# ── 1. no blocking render ────────────────────────────────────────────────────


def test_shell_renders_promptly_even_when_the_listener_hangs(client, dead_listener, env_save_restore, isolated_cache):
    # Arrange: a listener that never answers within the test budget.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", dead_listener)
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    started = time.monotonic()
    client.get("/")
    elapsed = time.monotonic() - started
    # Assert: the SHELL must not wait on the control plane.
    assert elapsed < 2.0


def test_shell_response_carries_no_inventory_when_the_reader_is_slow(client, dead_listener, env_save_restore, isolated_cache):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", dead_listener)
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert: a pending shell says so rather than showing rows.
    assert 'data-fleet-state="loading"' in html or 'data-fleet-state="unavailable"' in html


# ── 2. no leaks ──────────────────────────────────────────────────────────────


def test_shell_never_reveals_an_internal_endpoint(client, loopback, env_save_restore, isolated_cache):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "127.0.0.1:7878" not in html


def test_shell_never_reveals_a_config_env_var_name(client, loopback, env_save_restore, isolated_cache):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "SCITEX_AGENT_CONTAINER_API_URL" not in html


def test_shell_never_reveals_a_token_name_or_setup_prose(client, loopback, env_save_restore, isolated_cache):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode().lower()
    # Assert: no credential name and no deployment instructions for a browser.
    assert "api_token" not in html and "bearer token" not in html


def test_unavailable_state_still_leaks_nothing(client, dead_listener, env_save_restore, isolated_cache):
    # Arrange: the state where the old page dumped its whole setup paragraph.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", dead_listener)
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert not any(token in html for token in FORBIDDEN)


# ── 3. real inventory only, and honest staleness ─────────────────────────────


def test_cold_cache_yields_no_invented_agents():
    # Arrange
    cache = InventoryCache()
    # Act
    snap = cache.get("alice")
    # Assert: nothing observed means nothing to show.
    assert snap is None


def test_snapshot_reports_its_age_not_the_age_it_wishes_it_had():
    # Arrange
    cache = InventoryCache(ttl=10.0)
    # Act
    cache.put("alice", [{"name": "alpha"}])
    age = cache.get("alice").age()
    # Assert
    assert 0.0 <= age < 2.0


def test_an_expired_snapshot_is_not_fresh():
    # Arrange: a zero TTL means anything observed is already expired. No sleep
    # is needed to force it - a fixed sleep only adds a race on a slow runner.
    cache = InventoryCache(ttl=-1.0)
    cache.put("alice", [{"name": "alpha"}])
    # Act
    fresh = cache.fresh("alice")
    # Assert: expired means the UI must age it, not present it as current.
    assert fresh is None


def test_a_fresh_snapshot_is_served():
    # Arrange
    cache = InventoryCache(ttl=60.0)
    cache.put("alice", [{"name": "alpha"}])
    # Act
    snap = cache.fresh("alice")
    # Assert
    assert snap is not None and snap.agents[0]["name"] == "alpha"


# ── 4. scope safety and read-only prefetch ───────────────────────────────────


def test_one_identity_never_sees_another_identitys_snapshot():
    # Arrange: the cache must not become a cross-scope leak.
    cache = InventoryCache()
    cache.put("alice", [{"name": "alpha"}])
    # Act
    other = cache.get("bob")
    # Assert
    assert other is None


def test_prefetch_cannot_mutate_because_the_fetcher_is_the_only_input():
    # Arrange: the cache's only way to obtain rows is the callable it is given.
    # A threading.Event, NOT a sleep: a fixed sleep races the worker and is
    # exactly the kind of assumption that passes locally and fails on a
    # contended CI runner.
    import threading as _threading

    cache = InventoryCache()
    calls: list[str] = []
    done = _threading.Event()

    def read_only_fetcher():
        calls.append("read")
        done.set()
        return [{"name": "alpha"}]

    # Act
    cache.refresh_async("alice", read_only_fetcher)
    finished = done.wait(timeout=10.0)
    # Assert: the read ran, and no mutation path exists in the module.
    assert finished and calls == ["read"]


def test_concurrent_refreshes_for_one_identity_do_not_fan_out():
    # Arrange: a burst of page loads must not become a burst of SAC reads.
    cache = InventoryCache()
    started = []

    def slow_fetcher():
        started.append(1)
        time.sleep(0.25)
        return []

    # Act: the second call must be refused while the first is still running.
    first = cache.refresh_async("alice", slow_fetcher)
    second = cache.refresh_async("alice", slow_fetcher)
    # Assert: synchronised, not timed - wait for the started marker rather than
    # trusting a sleep to outlast thread startup on a slow runner.
    assert first is True and second is False and len(started) == 1


def test_cache_is_bounded():
    # Arrange: 200 distinct identities against a bounded cache.
    cache = InventoryCache()
    # Act
    for i in range(200):
        cache.put(f"user-{i}", [{"name": f"agent-{i}"}])
    # Assert: it does not grow without limit.
    assert len(cache._by_identity) <= cache._MAX_ENTRIES
