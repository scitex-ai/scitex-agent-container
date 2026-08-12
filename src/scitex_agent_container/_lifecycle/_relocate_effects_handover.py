"""HANDOVER: move the write lease source -> target. THE atomic point of a relocation.

:mod:`_relocate_lease` decided every rule and touched nothing;
:func:`.._state.state_db_relocation.save_lease` gave the rows a home. What was
missing was a holder: the refusal this module replaces said "nothing claims a
lease on the source's behalf at start-up, so there is no holder to hand FROM".

SO IT BOOTSTRAPS ONE, DELIBERATELY, AND SAYS SO. sac still does not claim a
lease when an agent starts, which means for any agent that has never been
through a relocation the store is empty.
:func:`._relocate_lease.check_write` is explicit that an empty store must be
resolved by a deliberate decision — bootstrap or refuse — and never inherited as
a default, because folding "no record" into either pole is how the split-brain
comes back. This bootstraps, records the bootstrap in the journal detail so
nobody later mistakes it for a lease the source had been holding all along, and
starts the fence at 0 so the handoff lands at 1.

A LEASE HELD BY A THIRD HOST IS A REFUSAL, NOT A BOOTSTRAP. A live lease naming
someone other than the source is the exact condition the lease was written to
detect, and helping past it would defeat the instrument. An EXPIRED one held by
another host is different and is re-claimed for the source first, so the fence
advances and the old holder is locked out by arithmetic rather than by trusting
its clock — the distinction :mod:`_relocate_lease` draws between what a TTL
decides and what a fence decides.

THE WRITE IS RE-READ. ``save_lease`` returning is the writer's opinion; who
holds the lease is the row's. This is the one step where a wrong answer means
two hosts believe they may write, so it is confirmed by reading the row back and
comparing both the holder and the fence.

NOTHING RENEWS THE LEASE YET, and the TTL is long for that reason. Stated
plainly because it bounds what the lease buys today: after expiry a fresh claim
is permitted, and what still excludes a stale holder is the FENCE, which only
ever increases. The absence of a renewer degrades the guarantee; it does not
remove it.
"""

from __future__ import annotations

import secrets

from ._relocate_execute import StepResult
from ._relocate_lease import claim, handoff

__all__ = ["LEASE_TTL_S", "HandoverEffects"]

#: Long, because nothing renews it. Twenty-four hours is comfortably longer than
#: any relocation and short enough that an abandoned claim does not outlive the
#: machine it was made on by weeks.
LEASE_TTL_S = 86400.0


class HandoverEffects:
    """Mixin: the HANDOVER phase. Expects ``RelocateAdapters``' attributes."""

    def hand_over_lease(self) -> StepResult:
        """Move the write lease from the source to the target, and confirm it moved."""
        from .._state.state_db_relocation import load_lease, save_lease

        now = self.now()
        held, note, refusal = self._holder_to_hand_from(now)
        if refusal is not None:
            return refusal

        moved, verdict = handoff(
            held,
            from_holder=self.from_host,
            token=held.token,
            fence=held.fence,
            to_holder=self.to_host,
            to_token=secrets.token_hex(16),
            now=now,
            ttl_s=LEASE_TTL_S,
        )
        if verdict.allowed is not True or moved is None:
            return StepResult(
                ok=False,
                detail=f"the lease did not move: {verdict.reason}",
                hint=(
                    "nothing was handed over and the source is still the owner of record; "
                    "read the relocation_leases row and settle it before re-running"
                ),
            )
        save_lease(moved)

        confirmed = load_lease(self.agent)
        if confirmed is None:
            return StepResult(
                ok=None,
                detail="the lease was written and could not be read back",
                hint=(
                    "read relocation_leases before doing anything else — the handover may "
                    "or may not have landed, and that is the one thing that must not stay "
                    "unknown"
                ),
            )
        if confirmed.holder != self.to_host or confirmed.fence != moved.fence:
            return StepResult(
                ok=False,
                detail=(
                    f"after the handoff the stored lease reads holder={confirmed.holder!r} "
                    f"fence={confirmed.fence}, not {self.to_host!r} at fence {moved.fence}"
                ),
                hint=(
                    "something else wrote that row; settle who owns this identity before "
                    "continuing"
                ),
            )
        self.lease_fence = confirmed.fence
        return StepResult(
            ok=True,
            detail=(
                f"the lease moved {self.from_host} -> {self.to_host} at fence "
                f"{confirmed.fence}; {self.from_host} is fenced out by arithmetic even "
                f"while its process could still be alive.{note} Nothing renews this lease "
                f"yet, so after {LEASE_TTL_S / 3600:.0f}h a fresh claim is permitted and "
                "the fence is what still excludes a stale holder"
            ),
        )

    def _holder_to_hand_from(self, now: float):
        """Resolve a source-held lease. Returns ``(lease, note, refusal)``.

        Exactly one of ``lease`` and ``refusal`` is meaningful. The note travels
        with the lease so the journal records HOW the source came to hold it —
        a bootstrapped claim and an inherited one are different facts, and a
        reader six months later cannot tell them apart from the fence alone.
        """
        from .._state.state_db_relocation import load_lease, save_lease

        held = load_lease(self.agent)
        if held is not None and held.holder == self.from_host:
            return held, "", None

        if held is not None and not held.is_expired(now):
            return (
                None,
                "",
                StepResult(
                    ok=False,
                    detail=(
                        f"the lease for {self.agent} is held by {held.holder!r} at fence "
                        f"{held.fence}, not by the source {self.from_host!r}"
                    ),
                    hint=(
                        "this is the split-brain the lease exists to catch. Find out what "
                        f"{held.holder!r} is doing with this identity before relocating; "
                        "do NOT force the handover"
                    ),
                ),
            )

        was_expired = held is not None
        granted, verdict = claim(
            held,
            agent=self.agent,
            holder=self.from_host,
            token=secrets.token_hex(16),
            now=now,
            ttl_s=LEASE_TTL_S,
        )
        if verdict.allowed is not True or granted is None:
            return (
                None,
                "",
                StepResult(
                    ok=False,
                    detail=(
                        f"the source's lease could not be "
                        f"{'re-claimed' if was_expired else 'bootstrapped'}: {verdict.reason}"
                    ),
                    hint="nothing was handed over; read the lease row before re-running",
                ),
            )
        save_lease(granted)
        if was_expired:
            note = (
                f" An EXPIRED lease held by {held.holder!r} was re-claimed for the source "
                f"at fence {granted.fence} before the handoff, so that holder is fenced "
                "out by arithmetic rather than by trusting its clock."
            )
        else:
            note = (
                f" The source's lease was BOOTSTRAPPED by this relocation at fence "
                f"{granted.fence}: sac does not claim one when an agent starts, so the "
                "store held nothing to hand from."
            )
        self.log.append(f"handover:{note.strip()}")
        return granted, note, None
