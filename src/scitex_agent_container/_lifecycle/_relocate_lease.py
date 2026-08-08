"""The write lease that makes two live instances of one agent unrepresentable.

sac declares ``cardinality: singleton`` in a spec and enforces it with nothing.
The identity lives in the spec and ``host:`` is just a field, so copying a spec
to another machine and starting it produces TWO agents under ONE identity, with
no error anywhere. That is the root of the 2026-08-07 card-store split-brain:
two instances, one id, two postgres stores, neither seeing the other's writes.

There is no "relocate" today — there is only copy-and-start, and copy-and-start
makes two. This module is the primitive the relocate verb hands off.

WHY A LEASE AND NOT A HANDSHAKE (operator, 2026-08-07). His first instinct was
a mutual handshake: source confirms the target is up, target confirms the source
is gone. Under a partition that either deadlocks (both wait) or double-commits
(both proceed) — the same failure it is meant to prevent. He agreed to this
instead: ONE token. Whoever holds it may write; nobody else can. Two holders is
not a race to be detected, it is a state that cannot be expressed. Relocation is
the ORDERED HANDOFF of that token, driven by the relocate command as
coordinator, never by agreement between peers.

WHY A FENCE AND NOT ONLY A TTL. A TTL alone assumes clocks agree. A source that
is paused (a stopped container, a suspended laptop, an NTP step) can wake up
believing its lease is still valid and write into a store the target now owns —
and it does so honestly, with a token that WAS legitimate. So every lease also
carries a FENCE: an integer that only ever increases, bumped on every handoff.
A writer presents its fence; anything below the current one is refused. The
stale holder is then locked out by arithmetic rather than by trusting its clock.
The TTL decides when a lease may be RECLAIMED; the fence decides who may WRITE.

SCOPE, and this is the operator's explicit requirement: the lease governs writes
to the SHARED store only. Each host's LOCAL store keeps accepting local work
while partitioned — every machine stays usable with no network — and
reconciliation settles it afterwards. A lease that stopped local work would make
a network outage into an outage of the whole fleet.

Pure and fully injectable: no clock, no I/O, no store. Every function takes the
current state and ``now`` and returns the next state plus a verdict, so the
state machine is unit-testable without two hosts, and the persistence layer
(state.db or the cards postgres) is a separate decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

__all__ = [
    "CODE_EXPIRED",
    "CODE_HELD_BY_OTHER",
    "CODE_NOT_HELD",
    "CODE_OK",
    "CODE_STALE_FENCE",
    "CODE_UNKNOWN",
    "CODE_WRONG_TOKEN",
    "Lease",
    "LeaseVerdict",
    "check_write",
    "claim",
    "handoff",
    "renew",
]

# Declared numeric codes. Documented meanings, stable across the boundary —
# never a bare bool, and never a small exit code overloaded with a domain
# meaning (1 and 2 already mean "generic failure" and "usage error" to every
# CLI framework, so a renamed verb would impersonate a success value).
CODE_OK: Final = 200
CODE_NOT_HELD: Final = 404
CODE_WRONG_TOKEN: Final = 403
CODE_HELD_BY_OTHER: Final = 409
CODE_EXPIRED: Final = 410
CODE_STALE_FENCE: Final = 412
#: The caller could not be told anything true — e.g. no lease record was
#: supplied at all. Distinct from "no", on purpose: see LeaseVerdict.allowed.
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class Lease:
    """Who currently holds the right to write for ``agent``.

    ``fence`` only ever increases. ``expires_at`` is an absolute epoch second,
    not a duration, so a record carries its own deadline and no reader has to
    know when it was issued.
    """

    agent: str
    holder: str
    token: str
    expires_at: float
    fence: int

    def __post_init__(self) -> None:
        # Validate where the value is BUILT, so a malformed lease fails here
        # rather than three layers downstream in whatever tried to write.
        if not self.agent:
            raise ValueError("Lease.agent must be non-empty")
        if not self.holder:
            raise ValueError("Lease.holder must be non-empty")
        if not self.token:
            raise ValueError(
                "Lease.token must be non-empty — an empty token would let any caller pass the token check"
            )
        if self.fence < 0:
            raise ValueError(f"Lease.fence must be >= 0, got {self.fence}")

    def is_expired(self, now: float) -> bool:
        """True once ``now`` has passed the deadline. Exactly AT the deadline
        the lease is already gone: a boundary that favours the holder would let
        two writers overlap on one shared instant."""
        return now >= self.expires_at


@dataclass(frozen=True)
class LeaseVerdict:
    """The answer to every lease question, in ONE shape.

    ``allowed`` is THREE-VALUED — ``True`` / ``False`` / ``None`` for "could not
    determine". Collapsing unknown into either pole is the bug this fleet ships
    most often: an unknown folded into ``True`` writes from two hosts, an
    unknown folded into ``False`` stops an agent that was fine.

    Callers must branch on ``allowed is True`` explicitly. ``if verdict:`` is a
    truthiness test on the dataclass, not on the field, and would be true even
    for a refusal — so this class deliberately does NOT define ``__bool__``.
    """

    allowed: bool | None
    code: int
    reason: str
    holder: str | None = None
    fence: int | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if self.allowed not in (True, False, None):
            raise ValueError(
                f"LeaseVerdict.allowed must be True/False/None, got {self.allowed!r}"
            )
        if not self.reason:
            raise ValueError(
                "LeaseVerdict.reason must be non-empty — a refusal with no reason is not actionable"
            )
        if self.allowed is True and self.code != CODE_OK:
            raise ValueError(
                f"LeaseVerdict: allowed=True must carry CODE_OK, got {self.code}"
            )
        if self.allowed is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"LeaseVerdict: allowed=None must carry CODE_UNKNOWN, got {self.code}"
            )


def _ok(lease: Lease, reason: str) -> LeaseVerdict:
    return LeaseVerdict(
        allowed=True,
        code=CODE_OK,
        reason=reason,
        holder=lease.holder,
        fence=lease.fence,
        expires_at=lease.expires_at,
    )


def _no(code: int, reason: str, lease: Lease | None = None) -> LeaseVerdict:
    return LeaseVerdict(
        allowed=False,
        code=code,
        reason=reason,
        holder=lease.holder if lease else None,
        fence=lease.fence if lease else None,
        expires_at=lease.expires_at if lease else None,
    )


def claim(
    lease: Lease | None,
    *,
    agent: str,
    holder: str,
    token: str,
    now: float,
    ttl_s: float,
) -> tuple[Lease | None, LeaseVerdict]:
    """Take an unheld or EXPIRED lease. Returns ``(lease, verdict)``.

    Refuses while another holder's lease is live — that refusal is the whole
    point, and it is why a second copy-and-start cannot quietly become a second
    writer. Re-claiming by the SAME holder is idempotent (it renews), so a
    retrying coordinator is never punished for retrying.

    The fence advances on every successful claim, including a reclaim after
    expiry: the previous holder may still be alive and unaware, and must be
    locked out even if its clock disagrees about when the lease ended.
    """
    if lease is not None and lease.agent != agent:
        # UNKNOWN, not a refusal: this record says nothing about `agent` either
        # way, and answering "no" would read as "someone else holds it" when in
        # fact we were handed the wrong record. The caller must fix its lookup,
        # not retry against a verdict it misread. Returns the lease untouched.
        return lease, LeaseVerdict(
            allowed=None,
            code=CODE_UNKNOWN,
            reason=(
                f"lease record describes agent {lease.agent!r}, not {agent!r} — "
                "cannot answer for an agent this record is not about"
            ),
        )
    if lease is not None and not lease.is_expired(now) and lease.holder != holder:
        return lease, _no(
            CODE_HELD_BY_OTHER,
            f"{agent}: held by {lease.holder!r} until {lease.expires_at} — {holder!r} may not write",
            lease,
        )
    next_fence = 0 if lease is None else lease.fence + 1
    granted = Lease(
        agent=agent,
        holder=holder,
        token=token,
        expires_at=now + ttl_s,
        fence=next_fence,
    )
    was = (
        "unheld"
        if lease is None
        else ("expired" if lease.is_expired(now) else "already ours")
    )
    return granted, _ok(
        granted, f"{agent}: claimed by {holder!r} (previous state: {was})"
    )


def renew(
    lease: Lease | None,
    *,
    holder: str,
    token: str,
    fence: int,
    now: float,
    ttl_s: float,
) -> tuple[Lease | None, LeaseVerdict]:
    """Extend a lease you already hold. Never resurrects an expired one.

    An expired lease must go back through :func:`claim` so the fence advances —
    letting renew revive it would hand a paused holder its old fence back, which
    is exactly the writer the fence exists to exclude.
    """
    if lease is None:
        return None, _no(CODE_NOT_HELD, f"no lease record for {holder!r} to renew")
    if lease.holder != holder:
        return lease, _no(
            CODE_HELD_BY_OTHER, f"held by {lease.holder!r}, not {holder!r}", lease
        )
    if lease.token != token:
        return lease, _no(
            CODE_WRONG_TOKEN, f"token mismatch for holder {holder!r}", lease
        )
    if lease.fence != fence:
        return lease, _no(
            CODE_STALE_FENCE, f"fence {fence} != current {lease.fence}", lease
        )
    if lease.is_expired(now):
        return lease, _no(
            CODE_EXPIRED,
            f"lease expired at {lease.expires_at}; re-claim it so the fence advances rather than renewing a dead lease",
            lease,
        )
    extended = replace(lease, expires_at=now + ttl_s)
    return extended, _ok(extended, f"{lease.agent}: renewed by {holder!r}")


def handoff(
    lease: Lease | None,
    *,
    from_holder: str,
    token: str,
    fence: int,
    to_holder: str,
    to_token: str,
    now: float,
    ttl_s: float,
) -> tuple[Lease | None, LeaseVerdict]:
    """Move the lease source -> target. THE single atomic point of a relocate.

    Everything before this is reversible and everything after it is a cleanup:
    before, the source still writes and the target is a harmless standby; after,
    the target writes and the source is locked out by the bumped fence even
    while its process is still alive. A crash between the two leaves exactly one
    writer either way, which is the property the whole phase order exists for.

    Refuses if the caller is not the current holder, presents the wrong token,
    or presents a stale fence. Handing off an EXPIRED lease is refused too: the
    coordinator has lost the authority it is trying to delegate, and should
    re-claim (advancing the fence) before trying again.
    """
    if lease is None:
        return None, _no(
            CODE_NOT_HELD, f"no lease to hand from {from_holder!r} to {to_holder!r}"
        )
    if lease.holder != from_holder:
        return lease, _no(
            CODE_HELD_BY_OTHER, f"held by {lease.holder!r}, not {from_holder!r}", lease
        )
    if lease.token != token:
        return lease, _no(
            CODE_WRONG_TOKEN, f"token mismatch for holder {from_holder!r}", lease
        )
    if lease.fence != fence:
        return lease, _no(
            CODE_STALE_FENCE, f"fence {fence} != current {lease.fence}", lease
        )
    if lease.is_expired(now):
        return lease, _no(
            CODE_EXPIRED,
            f"lease expired at {lease.expires_at}; the coordinator no longer holds what it is trying to hand over",
            lease,
        )
    moved = Lease(
        agent=lease.agent,
        holder=to_holder,
        token=to_token,
        expires_at=now + ttl_s,
        fence=lease.fence + 1,
    )
    return moved, _ok(
        moved,
        f"{lease.agent}: handed {from_holder!r} -> {to_holder!r} at fence {moved.fence}",
    )


def check_write(
    lease: Lease | None,
    *,
    holder: str,
    token: str,
    fence: int,
    now: float,
) -> LeaseVerdict:
    """May ``holder`` write to the SHARED store right now?

    Returns ``allowed=None`` when there is no lease record at all. That is not a
    refusal and not a permission — it is "this question has no answer here yet",
    and a caller that folds it into either pole reintroduces the split-brain.
    A fresh deployment with no lease row must decide deliberately (bootstrap a
    lease, or refuse and say so), never inherit a default.
    """
    if lease is None:
        return LeaseVerdict(
            allowed=None,
            code=CODE_UNKNOWN,
            reason=f"no lease record for {holder!r} — cannot determine write authority; bootstrap a lease or refuse deliberately",
        )
    if lease.holder != holder:
        return _no(
            CODE_HELD_BY_OTHER, f"held by {lease.holder!r}, not {holder!r}", lease
        )
    if lease.token != token:
        return _no(CODE_WRONG_TOKEN, f"token mismatch for holder {holder!r}", lease)
    if fence < lease.fence:
        return _no(
            CODE_STALE_FENCE,
            f"fence {fence} is behind current {lease.fence} — this holder was superseded while it was not looking",
            lease,
        )
    if fence > lease.fence:
        return _no(
            CODE_STALE_FENCE,
            f"fence {fence} is AHEAD of current {lease.fence} — the caller's record is not one this store issued",
            lease,
        )
    if lease.is_expired(now):
        return _no(
            CODE_EXPIRED,
            f"lease expired at {lease.expires_at}; stop writing and re-claim",
            lease,
        )
    return _ok(
        lease, f"{lease.agent}: {holder!r} holds the lease at fence {lease.fence}"
    )
