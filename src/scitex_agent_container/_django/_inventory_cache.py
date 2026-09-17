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

THREE PROPERTIES THAT ARE NOT NEGOTIABLE:

1. **READ-ONLY.** This cache calls only listing/status reads. There is no code
   path from here to a lifecycle verb, so a prefetch - which the Hub launcher
   may fire on idle/intent - cannot start, stop or restart anything. That is
   enforced by construction (the fetcher takes a read-only callable) rather
   than by discipline.

2. **NEVER FABRICATES.** A cold or expired cache yields NO rows, and the page
   then says "loading" or "last-known unknown" - it does not invent agents and
   does not present a stale snapshot as current. Every row carries the moment it
   was observed so the UI can age it honestly.

3. **SCOPE-SAFE.** Entries are keyed by IDENTITY, not globally. A snapshot taken
   for one operator's scope must never be served to another, or a cache becomes
   a cross-tenant leak. The key includes the identity string and the scope it
   was taken under.

TTL is deliberately short: this is a last-known hint to make first paint fast,
not a second source of truth. SAC remains the SSOT, and the snapshot is replaced
the moment a live read completes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

#: How long a snapshot may serve first paint. Short on purpose: long enough to
#: make a revisit instant, short enough that it is never mistaken for current.
DEFAULT_TTL_SECONDS = 45.0

#: A refresh that has been running longer than this is considered abandoned for
#: scheduling purposes (the thread may still finish; we just stop waiting on it
#: before starting the next).
REFRESH_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class Snapshot:
    """One scope's last-known inventory, with the moment it was observed."""

    identity: str
    agents: tuple[dict, ...]
    observed_at: float
    error: str = ""

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.observed_at)

    def is_fresh(self, now: float | None = None, ttl: float | None = None) -> bool:
        return self.age(now) <= (DEFAULT_TTL_SECONDS if ttl is None else ttl)


class InventoryCache:
    """Process-local, identity-scoped, short-TTL last-known inventory.

    Deliberately process-local and bounded: this is a fast-path hint, not a
    store. A restart loses it, which is correct - there is nothing here that
    cannot be re-read from SAC.
    """

    _MAX_ENTRIES = 64

    def __init__(self, *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._by_identity: dict[str, Snapshot] = {}
        self._refreshing: set[str] = set()

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, identity: str) -> Snapshot | None:
        """The last-known snapshot for THIS identity, fresh or not.

        Returns None when nothing has ever been observed for this identity -
        never another identity's rows.
        """
        with self._lock:
            return self._by_identity.get(identity)

    def fresh(self, identity: str) -> Snapshot | None:
        """The snapshot only if it is still within its TTL."""
        snap = self.get(identity)
        if snap is None or not snap.is_fresh(ttl=self.ttl):
            return None
        return snap

    # ── writes ───────────────────────────────────────────────────────────────
    def put(self, identity: str, agents: list[dict], *, error: str = "") -> Snapshot:
        """Record a freshly observed inventory for one identity."""
        snap = Snapshot(
            identity=identity,
            agents=tuple(agents),
            observed_at=time.time(),
            error=error,
        )
        with self._lock:
            if len(self._by_identity) >= self._MAX_ENTRIES:
                # Bounded: drop the oldest identity rather than grow forever.
                oldest = min(self._by_identity, key=lambda k: self._by_identity[k].observed_at)
                if oldest != identity:
                    self._by_identity.pop(oldest, None)
            self._by_identity[identity] = snap
        return snap

    def clear(self) -> None:
        with self._lock:
            self._by_identity.clear()
            self._refreshing.clear()

    # ── background refresh ───────────────────────────────────────────────────
    def refresh_async(
        self,
        identity: str,
        fetcher: Callable[[], list[dict]],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        """Kick a READ-ONLY background refresh; return whether one was started.

        At most one refresh per identity runs at a time, so a burst of page
        loads cannot fan out into a burst of control-plane reads. The fetcher is
        supplied by the caller and must be read-only - this class has no way to
        mutate, which is the point.
        """
        with self._lock:
            if identity in self._refreshing:
                return False
            self._refreshing.add(identity)

        def _run() -> None:
            try:
                agents = fetcher()
                self.put(identity, agents)
            except Exception as exc:  # a background refresh never raises outward
                if on_error is not None:
                    on_error(exc)
            finally:
                with self._lock:
                    self._refreshing.discard(identity)

        threading.Thread(target=_run, name=f"inventory-refresh-{identity[:16]}", daemon=True).start()
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
