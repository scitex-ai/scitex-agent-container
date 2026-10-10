"""A2A kick guard: every message kicks, exactly once per storm.

Operator direction (card ``sac-a2a-kick-on-message-20261006``): every A2A
message must kick the recipient agent — no unread-sitting — with dedupe /
rate-limit as the SOLE guardrail against kick storms and ping-pong loops.
A mailbox drop (DB row) is not delivery; only turn admission counts.

This module is the guardrail, deliberately separated from the wake
mechanism (:mod:`._channel_wake`, which POSTs to ``/v1/turn``) so the
storm policy is testable without any transport:

* **dedupe** — the same ``(recipient, msg_id)`` kicks at most once per
  ``dedupe_ttl_s``. Redelivered SSE events and retried dispatcher loops
  collapse into one kick.
* **rate limit** — at most ``max_kicks_per_window`` kicks per
  ``window_s`` per recipient. A peer that spams (or two peers that
  ping-pong completions) degrades to ``rate_limited`` instead of
  driving unbounded turns.

Outcomes (see :data:`KickDecision`): ``admit`` means "drive the turn";
``duplicate`` / ``rate_limited`` mean "skip the turn, the message is
still in the durable inbox". Skipping is never silent: the caller logs
the decision with the msg_id.

The ledger is per-process memory. A daemon restart resets it — a storm
that survives a restart gets at most one extra kick per message, which
the window cap bounds. Cross-process exactly-once is explicitly NOT
promised; the durable inbox remains the source of truth.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "KickDecision",
    "KickPolicy",
    "KickLedger",
]

#: What the guard decided for one kick request.
KickDecision = Literal["admit", "duplicate", "rate_limited"]


@dataclass(frozen=True)
class KickPolicy:
    """Storm-guardrail tuning. Defaults bound a storm, not normal traffic."""

    #: Same (recipient, msg_id) re-kicks only after this long.
    dedupe_ttl_s: float = 3600.0
    #: At most this many kicks per window per recipient.
    max_kicks_per_window: int = 10
    #: Sliding window for the rate cap.
    window_s: float = 60.0


@dataclass
class KickLedger:
    """In-memory dedupe + per-recipient token bucket.

    ``clock`` is injectable so tests drive time without sleeping.
    """

    policy: KickPolicy = field(default_factory=KickPolicy)
    clock: Callable[[], float] = time.monotonic
    _seen: dict[tuple[str, str], float] = field(default_factory=dict, repr=False)
    _hits: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def _prune(self, now: float) -> None:
        ttl = self.policy.dedupe_ttl_s
        self._seen = {
            key: at for key, at in self._seen.items() if now - at < ttl
        }
        cutoff = now - self.policy.window_s
        self._hits = {
            recipient: [at for at in hits if at > cutoff]
            for recipient, hits in self._hits.items()
        }
        self._hits = {
            recipient: hits for recipient, hits in self._hits.items() if hits
        }

    def check(self, recipient: str, msg_id: str | None) -> KickDecision:
        """Decide without recording. ``None`` msg_id cannot dedupe: admit."""
        now = self.clock()
        self._prune(now)
        if msg_id:
            if (recipient, msg_id) in self._seen:
                return "duplicate"
        hits = self._hits.get(recipient, [])
        if len(hits) >= self.policy.max_kicks_per_window:
            return "rate_limited"
        return "admit"

    def record(self, recipient: str, msg_id: str | None) -> None:
        """Record an admitted kick. Call only after :meth:`check` admitted."""
        now = self.clock()
        if msg_id:
            self._seen[(recipient, msg_id)] = now
        self._hits.setdefault(recipient, []).append(now)
