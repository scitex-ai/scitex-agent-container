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

A LEASE HELD BY A THIRD HOST THAT IS RUNNING THE AGENT IS A REFUSAL. That is the
exact condition the lease was written to detect and helping past it would defeat
the instrument. A row naming another host that is NOT running it is a different
thing entirely — this fleet writes the lease to the COORDINATOR's own db and the
coordinator is always the host being LEFT, so such a row is ordinarily the
record of the move that brought the agent HERE. It is re-claimed for the source,
the fence advances, and the old holder is locked out by arithmetic; the same is
done for an EXPIRED one. Which of those applies is decided by
:mod:`_relocate_lease_readiness`, and by the PREFLIGHT calling the same function
before anything is stopped — because this phase runs last, and its refusal used
to arrive with the agent already down (measured 2026-08-11, the canary's return
leg, exit 5 at this phase).

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
from ._relocate_lease_readiness import handoff_readiness
from ._relocate_liveness import observe_running
from ._relocate_shell import shell_for

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

        THE RULE IS NOT WRITTEN HERE. It is
        :func:`._relocate_lease_readiness.handoff_readiness`, and the preflight
        asks the identical question with the identical function before anything
        is stopped. That is the point: a gate that could pass while this phase
        refuses would be a gate that guarantees the agent goes down first.
        """
        from .._state.state_db_relocation import load_lease, save_lease

        held = load_lease(self.agent)
        holder_running = self._recorded_holder_running(held, now)
        ready = handoff_readiness(
            held,
            from_holder=self.from_host,
            now=now,
            recorded_holder_running=holder_running,
        )
        if ready.allowed is not True:
            return (
                None,
                "",
                StepResult(
                    ok=None if ready.allowed is None else False,
                    detail=ready.reason,
                    hint=(
                        f"look at {ready.holder!r}: is it running {self.agent}? Until "
                        "somebody does, this is undetermined rather than refused"
                        if ready.allowed is None
                        else "this is the split-brain the lease exists to catch, and it is "
                        "backed by an observation rather than a row. Settle which host "
                        "owns this identity before re-running; do NOT force the handover"
                    ),
                ),
            )
        if held is not None and held.holder == self.from_host:
            return held, "", None

        was_expired = held is not None and held.is_expired(now)
        superseded = held is not None and not was_expired
        granted, verdict = claim(
            held,
            agent=self.agent,
            holder=self.from_host,
            token=secrets.token_hex(16),
            now=now,
            ttl_s=LEASE_TTL_S,
            holder_absent=superseded,
        )
        if verdict.allowed is not True or granted is None:
            return (
                None,
                "",
                StepResult(
                    ok=False,
                    detail=(
                        f"the source's lease could not be "
                        f"{'re-claimed' if held is not None else 'bootstrapped'}: {verdict.reason}"
                    ),
                    hint="nothing was handed over; read the lease row before re-running",
                ),
            )
        save_lease(granted)
        if superseded:
            note = (
                f" The row named {held.holder!r} at fence {held.fence} and that host was "
                f"OBSERVED not running {self.agent}, so the lease was re-claimed for the "
                f"source at fence {granted.fence} — a record of a past handover, not a "
                "live writer."
            )
        elif was_expired:
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

    def _recorded_holder_running(self, held, now: float) -> bool | None:
        """Is the agent running on the host the LEASE ROW names? ``None`` = nobody asked.

        Only asked when the answer can change anything — a row naming the source
        itself, or an expired one, needs no third host observed, and probing one
        anyway would let an idle machine's ssh failure refuse a fine relocation.

        Uses :func:`._relocate_liveness.observe_running`, the same tmux question
        the runtime asks and the same one ``finish`` asks of both hosts. A probe
        that could not answer returns ``None``, which refuses — this is the one
        place where guessing "not running" would hand the lease away from a live
        writer.
        """
        if held is None or held.holder == self.from_host or held.is_expired(now):
            return None
        shell = shell_for(held.holder, local_host=self.local_host or None)
        running, why = observe_running(shell, self.agent, exec_fn=self.exec_fn)
        self.log.append(
            f"handover: the lease row names {held.holder}; {self.agent} there = "
            f"{running!r} ({why})"
        )
        return running
