"""Short-TTL server-side last-known inventory cache.

WHY THIS EXISTS. The fleet page blocked its entire render on the control-plane
read, which this fleet measures at 5-60s. So the shell - which needs nothing
from SAC - waited behind data it does not depend on. The operator saw ~10s of
blank; measured here at 60.06s.

THE SHAPE OF THE FIX, per the operator's ruling:
  * the SHELL must render before any inventory is fetched (never block routing
    on the listener),
  * the inventory is served from a SHORT-TTL last-known snapshot when one
    exists, so a returning visitor sees real rows in milliseconds,
  * refresh happens in the background and must never be able to mutate.

PROPERTIES THAT ARE NOT NEGOTIABLE:

1. **READ-ONLY.** This cache calls only listing/status reads. There is no code
   path from here to a lifecycle verb, so a prefetch - which the Hub launcher
   may fire on idle/intent - cannot start, stop or restart anything. That is
   enforced by construction (the fetcher takes a read-only callable) rather
   than by discipline.

2. **NEVER FABRICATES.** A cold or expired cache yields NO rows, and the page
   then says "loading" or "last-known unknown" - it does not invent agents and
   does not present a stale snapshot as current. Every row carries the moment it
   was observed so the UI can age it honestly.

3. **SCOPE-SAFE (dispatch-time scope key).** Entries are keyed by IDENTITY PLUS
   THE AUTHORIZATION SCOPE the read was DISPATCHED under (``own`` /
   ``crosshost`` from ``fleet_visibility``). A snapshot taken while an identity
   was granted cross-host visibility is NEVER served to it after the grant is
   revoked - the revoked scope resolves to a different key. The scope is
   captured when the read starts and carried through to the store: recomputing
   it at store time would file a granted read's rows under a key the revoked
   identity still resolves ("read authorized, grant revoked mid-flight").

4. **DEEP-COPIED BOTH WAYS.** ``put`` stores a ``copy.deepcopy`` of the row data,
   and every snapshot handed back out (``put``'s return, ``get``/``fresh``)
   is a fresh ``copy.deepcopy`` too. A ``frozen=True`` dataclass only pins its
   ATTRIBUTES (the tuple reference); without the copy the snapshot would retain
   mutable aliases that the producer - or any caller reading it - could still
   mutate after the fact.

5. **BOUNDED IN-FLIGHT, NEVER EXCEEDED.** At most ``_MAX_INFLIGHT`` background
   refreshes run across ALL identities at once, and the cap counts PHYSICAL
   WORKERS: a worker is charged when its read is dispatched and released only
   when its thread exits, so a SUPERSEDED worker keeps its slot for as long as
   it is still reading and is never traded for a ninth. A refresh older than
   ``REFRESH_STALE_SECONDS`` is abandoned only for its OWN key - a newer request
   for that key supersedes it *when there is a free slot* (and the superseded
   run's publish is dropped, so an old, slow read cannot overwrite a newer
   observation).

TTL is deliberately short: this is a last-known hint to make first paint fast,
not a second source of truth. SAC remains the SSOT, and the snapshot is replaced
the moment a live read completes.
"""

from __future__ import annotations

import copy
import itertools
import threading
import time
from dataclasses import dataclass
from typing import Callable

#: How long a snapshot may serve first paint. Short on purpose: long enough to
#: make a revisit instant, short enough that it is never mistaken for current.
DEFAULT_TTL_SECONDS = 45.0

#: A refresh running longer than this is ABANDONED FOR ITS OWN KEY: a newer
#: request for that key supersedes it instead of queueing behind it - but only
#: while a slot is free. The global cap counts PHYSICAL WORKERS (dispatched
#: until their thread exits), so a superseded read that is still on the wire
#: still holds its slot and a supersede can never admit one more than
#: ``_MAX_INFLIGHT`` concurrent reads.
REFRESH_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class Snapshot:
    """One (identity, scope) last-known inventory, with the moment it was observed.

    ``frozen=True`` pins the attributes; the row DATA is additionally
    deep-copied on store (see ``put``) so the tuple holds no shared mutable
    aliases. ``identity``/``scope`` record the key this snapshot was taken under.
    """

    identity: str
    scope: str
    agents: tuple[dict, ...]
    observed_at: float
    error: str = ""

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.observed_at)

    def is_fresh(self, now: float | None = None, ttl: float | None = None) -> bool:
        return self.age(now) <= (DEFAULT_TTL_SECONDS if ttl is None else ttl)


class InventoryCache:
    """Process-local, (identity, scope)-keyed, short-TTL last-known inventory.

    Deliberately process-local and bounded: this is a fast-path hint, not a
    store. A restart loses it, which is correct - there is nothing here that
    cannot be re-read from SAC.
    """

    #: Bound on SNAPSHOT entries kept (drop the oldest when exceeded).
    _MAX_ENTRIES = 64
    #: Bound on CONCURRENT background refreshes across all identities (B4).
    _MAX_INFLIGHT = 8

    def __init__(self, *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        # key: (identity, current-scope) -> Snapshot. Kept under the original
        # private name so existing tests of the bound are unchanged.
        self._by_identity: dict[tuple[str, str], Snapshot] = {}
        # key -> (run_id, start_monotonic). run_id guards the stale-reclaim race.
        self._inflight: dict[tuple[str, str], tuple[int, float]] = {}
        # PHYSICAL workers: charged when a read is dispatched, released only when
        # that worker's thread exits. A superseded worker is still reading, so it
        # keeps its slot - the cap can never be traded for a ninth read (B4).
        self._live_workers = 0
        self._run_seq = itertools.count()

    # ── keying ───────────────────────────────────────────────────────────────
    def _key(self, identity: str, scope: str | None = None) -> tuple[str, str]:
        """The (identity, scope) key: identity alone is NOT safe (B3).

        ``scope`` is passed in by a writer that captured it WHEN THE READ WAS
        DISPATCHED; only a caller with no such capture resolves the CURRENT
        scope. Recomputing a dispatched read's scope here is the defect - a
        grant revoked mid-flight would file the granted rows under the key the
        revoked identity still resolves.
        """
        if scope is None:
            from ._authorization import fleet_visibility

            scope = fleet_visibility(identity)
        return (identity, scope)

    @staticmethod
    def _copy(snapshot: "Snapshot") -> "Snapshot":
        """An independent copy of a snapshot, so callers cannot mutate stored data."""
        return copy.deepcopy(snapshot)

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, identity: str) -> Snapshot | None:
        """The last-known snapshot for THIS identity under its CURRENT scope.

        Returns None when nothing has been observed for (identity, scope) -
        never another identity's rows, and never a row cached under a scope the
        identity no longer holds (a revocation moves it to a different key). The
        snapshot handed out is a COPY: the caller cannot mutate the cache (B5).
        """
        with self._lock:
            snap = self._by_identity.get(self._key(identity))
        return None if snap is None else self._copy(snap)

    def fresh(self, identity: str) -> Snapshot | None:
        """The snapshot only if it is still within its TTL."""
        snap = self.get(identity)
        if snap is None or not snap.is_fresh(ttl=self.ttl):
            return None
        return snap

    # ── writes ───────────────────────────────────────────────────────────────
    def put(
        self,
        identity: str,
        agents: list[dict],
        *,
        error: str = "",
        scope: str | None = None,
        _fence_observed_at: float | None = None,
    ) -> Snapshot:
        """Record a freshly observed inventory for (identity, dispatch scope).

        The row data is DEEP-COPIED so the stored snapshot cannot be mutated
        through the producer's dicts, and the returned snapshot is itself a copy
        so it cannot be mutated into the cache (B5). ``scope`` is the
        AUTHORIZATION SCOPE CAPTURED AT DISPATCH by the caller (B3); only a
        direct caller with no capture resolves the identity's current scope.

        ``_fence_observed_at`` is the SUPERSEDE FENCE ``refresh_async`` passes -
        the moment its read was dispatched. When the stored snapshot was
        observed at or after that moment, a newer observation already exists and
        this (older) publication is DROPPED, returning the newer snapshot. The
        fence and the write are ONE critical section, so a newer put that lands
        while this read is still publishing cannot be overwritten afterwards:
        checking freshness and then storing it in a second step is the defect.
        """
        key = self._key(identity, scope)
        snap = Snapshot(
            identity=identity,
            scope=key[1],
            agents=tuple(copy.deepcopy(row) for row in agents),
            observed_at=time.time(),
            error=error,
        )
        with self._lock:
            if _fence_observed_at is not None:
                current = self._by_identity.get(key)
                if current is not None and current.observed_at >= _fence_observed_at:
                    return self._copy(current)
            if len(self._by_identity) >= self._MAX_ENTRIES:
                # Bounded: drop the oldest (identity, scope) snapshot rather
                # than grow forever.
                oldest_key = min(self._by_identity, key=lambda k: self._by_identity[k].observed_at)
                if oldest_key != key:
                    self._by_identity.pop(oldest_key, None)
            self._by_identity[key] = snap
        return self._copy(snap)

    def record_error(
        self,
        identity: str,
        *,
        error: str = "the control plane did not answer",
        scope: str | None = None,
    ) -> Snapshot:
        """Record a COMPLETED read failure for (identity, dispatch scope) (B1).

        Never clobbers a fresh successful snapshot: if a good last-known
        inventory is within TTL, keep serving it and ignore the transient
        background failure. Otherwise store the (already-redacted) error so the
        shell can show ``unavailable`` + Retry instead of a perpetual "loading".
        """
        key = self._key(identity, scope)
        with self._lock:
            existing = self._by_identity.get(key)
            if existing is not None and not existing.error and existing.is_fresh(ttl=self.ttl):
                return self._copy(existing)
        return self.put(identity, [], error=error, scope=scope)

    def clear(self) -> None:
        """Drop the stored snapshots and the in-flight markers.

        Workers already dispatched keep their physical slot: they are still
        reading, so clearing bookkeeping must not free a slot the cap is
        accountable for (they release it when their threads exit).
        """
        with self._lock:
            self._by_identity.clear()
            self._inflight.clear()

    # ── background refresh ───────────────────────────────────────────────────
    def refresh_async(
        self,
        identity: str,
        fetcher: Callable[[], list[dict]],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        """Kick a READ-ONLY background refresh; return whether one was started.

        At most one refresh per (identity, dispatch scope) runs at a time, AND at
        most ``_MAX_INFLIGHT`` PHYSICAL workers run across ALL identities (B4).
        A refresh older than ``REFRESH_STALE_SECONDS`` is abandoned only for its
        OWN key: a newer request for that key supersedes it instead of queueing
        behind it - but a superseded read is still READING, so it keeps its slot
        until its thread exits and a supersede is refused at the cap exactly like
        a new key. Nothing here trades a live worker for a ninth read. The
        fetcher is supplied by the caller and must be read-only - this class has
        no way to mutate, which is the point.
        """
        # The authorization scope is captured HERE, at dispatch: a grant revoked
        # while this refresh is on the wire must not file its rows under the
        # scope the identity resolves afterwards (B3).
        key = self._key(identity)
        dispatch_scope = key[1]
        dispatched_at = time.time()
        now = time.monotonic()
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None and (now - existing[1]) < REFRESH_STALE_SECONDS:
                # Already in flight for this key and not abandoned: refuse.
                return False
            if self._live_workers >= self._MAX_INFLIGHT:
                # At the cap: refuse, whether this is a new key or a supersede
                # of an abandoned-looking one. The superseded worker has not
                # exited, so admitting a replacement here would start one more
                # read than the cap allows (B4).
                return False
            run_id = next(self._run_seq)
            self._inflight[key] = (run_id, now)
            # Charged at dispatch, released when the thread exits: the slot
            # belongs to the WORKER, not to the marker it may have replaced.
            self._live_workers += 1

        def _run() -> None:
            try:
                agents = fetcher()
                # A SUPERSEDED read must not publish. The fence is the moment
                # this read was dispatched and it is checked INSIDE the same
                # critical section that stores, so a newer observation that
                # lands while this read is still publishing is not overwritten
                # by it (check-then-put is the defect: B4).
                self.put(
                    identity,
                    agents,
                    scope=dispatch_scope,
                    _fence_observed_at=dispatched_at,
                )
            except Exception as exc:  # a background refresh never raises outward
                if on_error is not None:
                    on_error(exc)
            finally:
                with self._lock:
                    # Discard our marker only if we still own it (a stale-reclaim
                    # for this key may have replaced our run with a newer one).
                    cur = self._inflight.get(key)
                    if cur is not None and cur[0] == run_id:
                        self._inflight.pop(key, None)
                    self._live_workers -= 1

        try:
            threading.Thread(
                target=_run, name=f"inventory-refresh-{identity[:16]}", daemon=True
            ).start()
        except Exception:  # a worker that never started holds no slot
            with self._lock:
                self._live_workers -= 1
                cur = self._inflight.get(key)
                if cur is not None and cur[0] == run_id:
                    self._inflight.pop(key, None)
            raise
        return True


#: The process-wide cache the views use.
CACHE = InventoryCache()


__all__ = [
    "CACHE",
    "DEFAULT_TTL_SECONDS",
    "InventoryCache",
    "REFRESH_STALE_SECONDS",
    "Snapshot",
]
