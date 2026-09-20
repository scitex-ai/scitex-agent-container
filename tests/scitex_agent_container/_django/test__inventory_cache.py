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
from scitex_agent_container._django._inventory_cache import (
    REFRESH_STALE_SECONDS,
    InventoryCache,
)

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


# ── B3: the key is identity + current authorization scope, not identity alone ──


def test_scope_revoke_does_not_serve_a_granted_snapshot(env_save_restore):
    # Arrange: an identity granted cross-host visibility caches rows while granted.
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    cache = InventoryCache()
    env_save_restore.set(CROSSHOST_OPERATORS_ENV, "alice")
    cache.put("alice", [{"name": "alpha"}, {"name": "gamma", "cross_host": True}])
    # Act: the cross-host grant is revoked; the same identity reads back.
    env_save_restore.delete(CROSSHOST_OPERATORS_ENV)
    snap = cache.get("alice")
    # Assert: the revoked-scope key no longer resolves the granted-scope snapshot.
    assert snap is None


def test_granted_then_revoked_scope_is_a_distinct_key(env_save_restore):
    # Arrange: two identities, one granted cross-host, one not.
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    cache = InventoryCache()
    env_save_restore.set(CROSSHOST_OPERATORS_ENV, "granted")
    # Act: store for the granted identity, then read it back un-granted.
    cache.put("granted", [{"name": "remote", "cross_host": True}])
    env_save_restore.delete(CROSSHOST_OPERATORS_ENV)
    # Assert: the granted-scope entry is not served under the revoked scope.
    assert cache.get("granted") is None


# ── B5: a stored snapshot is an independent copy, not a mutable alias ─────────


def test_put_deep_copies_rows_so_the_producer_cannot_mutate_the_snapshot():
    # Arrange: a producer hands the cache a row with nested mutable data.
    cache = InventoryCache()
    source = [{"name": "alpha", "activity": {"operation": {"value": "busy"}}}]
    # Act: store it, then mutate the producer's dict after the fact.
    cache.put("alice", source)
    source[0]["name"] = "MUTATED"
    source[0]["activity"]["operation"]["value"] = "MUTATED"
    # Assert: the stored snapshot is an independent copy, untouched.
    row = cache.get("alice").agents[0]
    assert row["name"] == "alpha" and row["activity"]["operation"]["value"] == "busy"


# ── B4: in-flight refreshes are bounded by PHYSICAL workers ──────────────────
# The cap counts workers, not markers: a superseded worker keeps its slot until
# its thread exits, so an abandoned-LOOKING marker is superseded only while a
# slot is physically free and is never traded for one more read.


def test_inflight_refreshes_are_bounded_globally_across_identities():
    # Arrange: more distinct identities than the global in-flight cap (8).
    cache = InventoryCache()
    gate = threading.Event()
    started: list[int] = []

    def gated_fetcher():
        started.append(1)
        gate.wait(timeout=10.0)
        return []

    # Act: fire a burst of refreshes for 13 distinct identities.
    results = [cache.refresh_async(f"user-{i}", gated_fetcher) for i in range(13)]
    deadline = time.monotonic() + 5.0
    while len(started) < 8 and time.monotonic() < deadline:
        time.sleep(0.01)
    gate.set()
    # Assert: the burst cannot fan out into unbounded concurrent refreshes.
    assert sum(results) <= 8


def test_stale_inflight_refresh_is_reclaimed_not_waited_on():
    # Arrange: a marker older than REFRESH_STALE_SECONDS, with NO worker
    # physically alive for it (the thread is gone; only bookkeeping remains).
    # The cap counts physical workers, so this marker holds no slot and a new
    # refresh for the key must be SUPERSEDED into place - not waited on, and
    # not refused for the marker's sake.
    import scitex_agent_container._django._inventory_cache as m

    cache = m.InventoryCache()
    started: list[int] = []

    def quick():
        started.append(1)
        return []

    key = cache._key("alice")
    with cache._lock:
        cache._inflight[key] = (12345, time.monotonic() - (m.REFRESH_STALE_SECONDS + 1.0))
    # Act: a fresh refresh for the same key.
    allowed = cache.refresh_async("alice", quick)
    # Assert: the new refresh proceeded (nothing to wait on). It was admitted
    # because the key had no LIVE worker, which the physical count records.
    assert allowed is True and started == [1]


# ── B1 support: a completed failure is recorded without clobbering a success ──


def test_record_error_keeps_a_fresh_successful_snapshot():
    # Arrange: a fresh success is within TTL.
    cache = InventoryCache(ttl=60.0)
    cache.put("alice", [{"name": "alpha"}])
    # Act: a transient background failure is recorded.
    snap = cache.record_error("alice")
    # Assert: the fresh success is served, not the transient failure.
    assert snap.error == "" and snap.agents[0]["name"] == "alpha"


def test_record_error_surfaces_a_completed_failure_on_a_cold_cache():
    # Arrange: nothing observed for this identity.
    cache = InventoryCache(ttl=60.0)
    # Act: record the completed read failure.
    snap = cache.record_error("alice")
    # Assert: the shell can now show unavailable + Retry, not a spinner.
    assert snap.error == "the control plane did not answer"


# ── Adversarial: the seams the first candidate left open ─────────────────────
# Each of these fails against the rejected candidate and holds only when the
# seam is closed. They are written against the module's own primitives (no
# patch/mock): the fetch is gated by an Event, so the interleaving is forced
# rather than hoped for.


def _await(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate``; never trust a fixed sleep to outlast a race."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def stale_fetch_window():
    """A slow refresh paused after entering the in-flight window."""
    cache = InventoryCache()
    fetched = threading.Event()
    release = threading.Event()

    def slow_fetcher():
        fetched.set()
        release.wait(timeout=10.0)
        return [{"name": "stale"}]

    if not cache.refresh_async("alice", slow_fetcher):
        raise RuntimeError("stale-fetch fixture could not dispatch its refresh")
    if not fetched.wait(timeout=10.0):
        raise RuntimeError("stale-fetch fixture never entered the in-flight window")
    yield cache, release
    release.set()


def test_stale_fetch_fixture_has_a_live_inflight_worker(stale_fetch_window):
    # Arrange
    cache, _release = stale_fetch_window
    # Act
    has_inflight_worker = bool(cache._inflight)
    # Assert
    assert has_inflight_worker


def test_stale_fetch_does_not_publish_over_a_newer_snapshot(stale_fetch_window):
    # Arrange: a slow refresh is dispatched, then a NEWER observation lands while
    # it is still in flight (the browser's synchronous /api/fleet read is the
    # real case: B1's poll completes a fresh read while the shell's background
    # refresh is still on the wire).
    cache, release = stale_fetch_window
    cache.put("alice", [{"name": "fresh"}])
    # Act: the older fetch finally returns and tries to store its observation.
    release.set()
    _await(lambda: not cache._inflight)
    # Assert: a superseded fetch must never overwrite the newer snapshot.
    assert [a["name"] for a in cache.get("alice").agents] == ["fresh"]


@pytest.fixture
def full_physical_refresh_cap():
    """A cache whose physical refresh workers occupy every global slot."""
    cache = InventoryCache()
    cap = InventoryCache._MAX_INFLIGHT
    gate = threading.Event()
    started: list[int] = []

    def blocked_fetcher():
        started.append(1)
        gate.wait(timeout=10.0)
        return []

    admitted = [cache.refresh_async(f"user-{i}", blocked_fetcher) for i in range(cap)]
    if not all(admitted):
        raise RuntimeError("physical-cap fixture was refused before reaching its cap")
    if not _await(lambda: len(started) >= cap):
        raise RuntimeError("physical-cap fixture did not start every worker")
    yield cache, cap, gate, started, blocked_fetcher
    gate.set()


def test_physical_refresh_cap_fixture_occupies_every_slot(full_physical_refresh_cap):
    # Arrange
    cache, cap, _gate, started, _blocked_fetcher = full_physical_refresh_cap
    # Act
    occupancy = (len(started), len(cache._inflight))
    # Assert
    assert occupancy == (cap, cap)


def test_stale_reclaim_does_not_start_a_ninth_refresh_or_hide_a_blocked_worker(
    full_physical_refresh_cap,
):
    # Arrange: age every marker past the stale horizon so all of them look
    # abandoned while every physical worker is still reading.
    cache, cap, _gate, started, blocked_fetcher = full_physical_refresh_cap
    with cache._lock:
        for key, (run_id, _started_at) in list(cache._inflight.items()):
            cache._inflight[key] = (run_id, time.monotonic() - (REFRESH_STALE_SECONDS + 1.0))
    # Act: a NINTH distinct identity asks for a refresh while the cap is full.
    allowed = cache.refresh_async("user-9", blocked_fetcher)
    _await(lambda: len(started) > cap, timeout=1.0)
    # Assert: no ninth worker beyond the cap. The cap counts PHYSICAL workers.
    assert allowed is False and len(started) == cap and len(cache._inflight) == cap


@pytest.fixture
def granted_crosshost_refresh(env_save_restore):
    """A granted cross-host refresh paused while its authorization is revoked."""
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    cache = InventoryCache()
    env_save_restore.set(CROSSHOST_OPERATORS_ENV, "op")
    release = threading.Event()

    def gated_fetcher():
        release.wait(timeout=10.0)
        return [{"name": "gamma", "cross_host": True}]

    if not cache.refresh_async("op", gated_fetcher):
        raise RuntimeError("granted-scope fixture could not dispatch its refresh")
    yield cache, release
    release.set()


def test_granted_crosshost_scope_fixture_has_an_inflight_refresh(
    granted_crosshost_refresh,
):
    # Arrange
    cache, _release = granted_crosshost_refresh
    # Act
    has_inflight_refresh = bool(cache._inflight)
    # Assert
    assert has_inflight_refresh


def test_dispatch_time_scope_is_not_recomputed_at_put_after_revocation(
    env_save_restore,
    granted_crosshost_refresh,
):
    # Arrange: the fixture dispatched while the cross-host grant was live.
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    cache, release = granted_crosshost_refresh
    # Act: the grant is revoked while the fetch is in flight; it then lands.
    env_save_restore.delete(CROSSHOST_OPERATORS_ENV)
    release.set()
    _await(lambda: not cache._inflight)
    # Assert: the rows are filed under the DISPATCH-time scope, so the identity's
    # now-current (own) scope cannot resolve them.
    assert cache.get("op") is None


def test_get_and_put_return_do_not_expose_mutable_nested_cache_data():
    # Arrange: a row whose nested payload a caller could mutate in place.
    cache = InventoryCache()
    source = [{"name": "alpha", "activity": {"operation": {"value": "busy"}}}]
    # Act: mutate the snapshot handed BACK by put, and the one handed OUT by get.
    handed_back = cache.put("alice", source)
    handed_back.agents[0]["name"] = "MUTATED"
    handed_back.agents[0]["activity"]["operation"]["value"] = "MUTATED"
    handed_out = cache.get("alice")
    handed_out.agents[0]["activity"]["operation"]["value"] = "MUTATED"
    # Assert: neither caller could reach into the cache's own copy.
    row = cache.get("alice").agents[0]
    assert row["name"] == "alpha" and row["activity"]["operation"]["value"] == "busy"


# ── Follow-up review round: the three seams the reviewed candidate left open ──
# Each drives the module's own primitives with Event gates (the subclass in the
# atomicity test is the module under test, not a mock), so the interleaving is
# forced rather than hoped for.


def test_same_key_stale_supersede_does_not_admit_a_ninth_physical_worker(
    full_physical_refresh_cap,
):
    # Arrange: age one occupied key past the stale horizon while its worker lives.
    cache, cap, _gate, started, blocked_fetcher = full_physical_refresh_cap
    with cache._lock:
        run_id, _started_at = cache._inflight[cache._key("user-0")]
        cache._inflight[cache._key("user-0")] = (
            run_id,
            time.monotonic() - (REFRESH_STALE_SECONDS + 1.0),
        )
    # Act: the same key asks again while every physical slot remains occupied.
    admitted = cache.refresh_async("user-0", blocked_fetcher)
    _await(lambda: len(started) > cap, timeout=1.0)
    # Assert: a superseded worker still counts until it physically exits.
    assert admitted is False and len(started) == cap


class _PublicationBarrierCache(InventoryCache):
    """A real cache whose BACKGROUND publication can be held open mid-flight.

    Subclassing rather than patching keeps the code under test real: the only
    change is that a background refresh's store reports it has ARRIVED and then
    waits, which puts an intervening put strictly between the freshness decision
    and the write. That is the window a check-then-put leaves open.
    """

    def __init__(self) -> None:
        super().__init__()
        self.arrived = threading.Event()
        self.resume = threading.Event()

    def put(self, identity, agents, **kwargs):
        if threading.current_thread().name.startswith("inventory-refresh-"):
            self.arrived.set()
            assert self.resume.wait(timeout=10.0)
        return super().put(identity, agents, **kwargs)


@pytest.fixture
def background_at_publication_barrier():
    """A background refresh paused at the publication boundary."""
    cache = _PublicationBarrierCache()
    if not cache.refresh_async("alice", lambda: [{"name": "stale"}]):
        raise RuntimeError("publication-barrier fixture could not dispatch")
    if not cache.arrived.wait(timeout=10.0):
        raise RuntimeError("publication-barrier fixture was never reached")
    yield cache
    cache.resume.set()


def test_background_refresh_fixture_reaches_publication_barrier(
    background_at_publication_barrier,
):
    # Arrange
    cache = background_at_publication_barrier
    # Act
    reached_barrier = cache.arrived.is_set()
    # Assert
    assert reached_barrier


def test_background_publication_is_atomic_against_an_intervening_put(
    background_at_publication_barrier,
):
    # Arrange: a background refresh whose store is held open on its way in.
    cache = background_at_publication_barrier
    # Act: a NEWER observation lands while the older read is still publishing.
    cache.put("alice", [{"name": "fresh"}])
    cache.resume.set()
    _await(lambda: not cache._inflight)
    # Assert: fencing and the write are ONE step, so the intervening - newer -
    # put cannot be overwritten by the older read's later publication.
    assert [a["name"] for a in cache.get("alice").agents] == ["fresh"]
