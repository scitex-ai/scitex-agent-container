"""Would the handover be allowed to run? The same question, asked before anything stops.

:mod:`_relocate_lease` is the state machine — claim, renew, hand off, may-I-write.
This is one PREDICATE ABOUT that machine, and it lives apart from it because it
is read by a different layer at a different moment: the PREFLIGHT, while the
agent is still up, about a phase that will not run for another five steps.

WHY IT EXISTS AT ALL. Measured 2026-08-11, the canary's RETURN leg: the
relocation refused at ``handover`` with exit 5 — correctly, safely, and after
``source_stop``, after the transport was byte-verified, after the standby booted
and after the handshake passed. The agent was down on the source and the answer
had been knowable before any of it. So the rule moved here, and both the gate and
the phase call it: two copies would be one refactor away from a preflight that
passes on a handover that will refuse, which is worse than having no preflight.

WHAT THE CANARY ACTUALLY HIT, because the fix follows from it. The lease is
written to the COORDINATOR's local state db, and the coordinator is always the
host being LEFT. So after A -> B, the row on A reads ``holder=B`` and B's own
store never hears about it. When B later moves back, the coordinator stands on B,
reads B's store, and finds a row from an EARLIER move that reads ``holder=A``.
Nothing in that row says A is writing today; it says a relocation once handed A
the lease, on a machine nobody has asked since.

    ywata-note-win state db:    holder=scitex-compute-04  fence 1
    scitex-compute-04 state db: holder=ywata-note-win     fence 1

Both rows are true and neither is current. Reading either as "another host holds
write authority" turns every SECOND move of every agent into a refusal.

THE FIX IS EVIDENCE, NOT A FLAG. A live row naming another holder is not the
split-brain by itself. The split-brain is another host RUNNING THIS AGENT — and
that is a thing to go and LOOK at, with the same tmux probe the runtime uses,
rather than something to infer from a row or to wave past with ``--force``. So
the recorded holder's liveness is an INPUT here, three-valued like everything
else in this feature, and "nobody looked" is an answer that refuses.

This also settles where residency belongs, at least for now, and the answer is
the honest one rather than the tidy one: the destination does NOT need to learn.
Per-host postgres is the intended topology and the cross-host sync primitive does
not exist yet, so a lease row means "what the last relocation I coordinated did",
and it is only ever authoritative WHEN COMBINED with an observation of the host
it names. Once a sync primitive exists the observation becomes a fast path rather
than the proof; until then it is the proof, and it is a better one than a TTL —
which is a guess about liveness — has ever been.

Pure: a stored lease, a clock and an observation in, a verdict out.
"""

from __future__ import annotations

from ._relocate_lease import (
    CODE_HELD_BY_OTHER,
    CODE_OK,
    CODE_UNKNOWN,
    Lease,
    LeaseVerdict,
    _no,
    _ok,
)

__all__ = ["handoff_readiness"]


def handoff_readiness(
    lease: Lease | None,
    *,
    from_holder: str,
    now: float,
    recorded_holder_running: bool | None = None,
) -> LeaseVerdict:
    """Could :func:`.._relocate_lease.handoff` run right now, from ``from_holder``?

    The branches, and what each one means for the move about to happen:

        no row                          bootstrap. sac does not claim a lease
                                        when an agent starts, so an agent that
                                        has never relocated has no row at all.
        the source already holds it     hand it over. The ordinary second move.
        held by another, EXPIRED        re-claim for the source; the fence
                                        advances and the old holder is locked
                                        out by arithmetic rather than by
                                        trusting anyone's clock.
        held by another, RUNNING it     REFUSE. Two live loops under one
                                        identity, now backed by an observation
                                        instead of a row. A coordinator must not
                                        settle that by out-voting another host.
        held by another, NOT running    proceed, re-claiming as above. The row
                                        records a past handover, not a present
                                        writer.
        held by another, unobserved     UNKNOWN. Go and look at that host.

    ``recorded_holder_running`` defaults to ``None`` on purpose: a caller that
    has not observed the other host has established nothing, and inheriting a
    pass for a measurement it never took is the shape of every bug this feature
    was written about. It is never consulted when the row names the source or
    when the lease has expired — in both cases no third host needs observing,
    and asking for one would make a fine relocation depend on an idle probe.

    Reports READINESS only. It moves nothing, writes nothing, and takes no clock
    of its own.
    """
    if not from_holder:
        raise ValueError(
            "handoff_readiness needs the source holder — 'may this host hand the "
            "lease over' cannot be answered without naming the host"
        )
    if lease is None:
        return LeaseVerdict(
            allowed=True,
            code=CODE_OK,
            reason=(
                f"no lease record exists; {from_holder!r}'s lease will be BOOTSTRAPPED "
                "at fence 0 and handed over at fence 1 (sac does not claim a lease when "
                "an agent starts)"
            ),
        )
    if lease.holder == from_holder:
        return _ok(
            lease,
            f"{lease.agent}: the source {from_holder!r} already holds the lease at fence "
            f"{lease.fence}",
        )
    if lease.is_expired(now):
        return _ok(
            lease,
            (
                f"{lease.agent}: the lease is held by {lease.holder!r} but EXPIRED at "
                f"{lease.expires_at}; it will be re-claimed for {from_holder!r} and the "
                "fence will advance, locking the old holder out by arithmetic"
            ),
        )
    if recorded_holder_running is None:
        return LeaseVerdict(
            allowed=None,
            code=CODE_UNKNOWN,
            reason=(
                f"{lease.agent}: the lease is held by {lease.holder!r} at fence "
                f"{lease.fence}, and whether {lease.holder!r} is RUNNING this agent was "
                "not observed — a row naming another holder is a record of a past "
                "handover until somebody looks at that host"
            ),
        )
    if recorded_holder_running:
        return _no(
            CODE_HELD_BY_OTHER,
            (
                f"{lease.agent}: the lease is held by {lease.holder!r} at fence "
                f"{lease.fence} AND {lease.holder!r} is running this agent — two live "
                f"instances under one identity, not a stale row. {from_holder!r} may not "
                "hand over what another live holder owns"
            ),
            lease,
        )
    return _ok(
        lease,
        (
            f"{lease.agent}: the lease row names {lease.holder!r} at fence {lease.fence}, "
            f"but {lease.holder!r} was OBSERVED not running this agent — the row records "
            f"a past handover, not a present writer. It will be re-claimed for "
            f"{from_holder!r} and the fence will advance"
        ),
    )
