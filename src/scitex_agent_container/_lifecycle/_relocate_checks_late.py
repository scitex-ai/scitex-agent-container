"""The two checks that used to fire AFTER the agent was already stopped.

:mod:`_relocate_checks` holds the predicates learned from doing the move by
hand. These two were learned from watching the move RUN: on the 2026-08-11
canary both of them refused correctly, both refused safely, and both refused
late — one at ``handover``, one at the target's own ``sac agents start``, and by
then ``source_stop`` had already happened. An agent whose relocation cannot
succeed must be told so while it is still running, so the questions moved here
and the phases now ask a decision that was already made.

    lease_holdable        the handover's own precondition, asked before
                          anything stops. Shares ONE predicate with the phase
                          (:func:`.._relocate_lease.handoff_readiness`) so the
                          gate cannot pass on a phase that will refuse.
    target_start_accepts  whether the TARGET's ``sac agents start`` would take
                          this agent at all. Preflight had eleven checks about
                          the target and not one of them asked the target's own
                          start command anything.

WHY ``target_start_accepts`` IS NOT A REIMPLEMENTATION OF THE DRIFT GUARD. The
guard is the code that will refuse the boot, so it is the only thing entitled to
say whether it would; the probe asks the target's OWN sac and this file reads
back its verdict. A second copy of the rule here would pass on exactly the day
the real one changed. What this file does own is the CONSEQUENCE — the guard
refuses on BEHIND and DIVERGED and never on AHEAD, and a relocation must refuse
in exactly the same places or it has invented a different policy.

NO I/O, like its sibling. Facts in, :class:`Check`s out.
"""

from __future__ import annotations

from typing import Final

from ._relocate_lease_readiness import handoff_readiness
from ._relocate_preflight_facts import Check, LeaseFacts, TargetFacts

__all__ = [
    "CHECK_LEASE",
    "CHECK_TARGET_START",
    "STALE_STATES",
    "check_lease_holdable",
    "check_target_start",
]

CHECK_LEASE: Final = "lease_holdable"
CHECK_TARGET_START: Final = "target_start_accepts"

#: The :class:`.._drift._status.DriftState` values that make the target's start
#: refuse. Mirrors ``DriftStatus.is_stale`` — BEHIND and DIVERGED only. AHEAD is
#: deliberately absent: the spec about to launch is the newest one that exists,
#: it merely has not propagated, and refusing on it would ground every host that
#: legitimately carries local commits.
STALE_STATES: Final[frozenset[str]] = frozenset({"behind", "diverged"})


def check_lease_holdable(facts: LeaseFacts, from_host: str, agent: str) -> Check:
    """Can the source hand the write lease over — asked before the source stops.

    Measured 2026-08-11, the canary's RETURN leg: exit 5 at ``handover``, after
    ``source_stop``, after the transport was byte-verified, after the standby
    booted and after the handshake passed. The refusal itself was right. Its
    TIMING left an agent down on a machine whose lease row had been written by a
    relocation the day before.

    The rule is :func:`.._relocate_lease.handoff_readiness` and it is not
    restated here — this function only supplies the facts to it and turns the
    verdict into a :class:`Check`, so the gate and the phase cannot disagree.
    """
    where = from_host or "the source"
    if not facts.read:
        return Check(
            name=CHECK_LEASE,
            ok=None,
            detail="the lease store was not read",
            hint=(
                "read the agent's record from relocation_leases in the PostgreSQL "
                "store (_state.relocation_pg.load_lease) before deciding — NOT "
                "the retired local file, which still answers from the row left behind by "
                "the 2026-08-28 cutover and would name a holder and a fence that "
                "moved on. The handover needs a lease it can hand FROM, and "
                "discovering that after the agent has been stopped is the one thing "
                "this check exists to stop"
            ),
        )
    if not from_host:
        return Check(
            name=CHECK_LEASE,
            ok=None,
            detail="the host being LEFT is not known, so no lease question can be asked about it",
            hint=(
                "resolve the source host first — 'may this host hand the lease over' "
                "has no answer until the host is named"
            ),
        )
    if facts.now is None:
        return Check(
            name=CHECK_LEASE,
            ok=None,
            detail=f"the lease row for {agent} was read with no clock, so its expiry is undecidable",
            hint=(
                "supply the moment the store was read. A lease carries an absolute "
                "deadline, and whether it has passed is not a property of the row alone"
            ),
        )

    verdict = handoff_readiness(
        facts.lease,  # type: ignore[arg-type]
        from_holder=from_host,
        now=facts.now,
        recorded_holder_running=facts.recorded_holder_running,
    )
    store = f" (store: {facts.store})" if facts.store else ""
    seen = f" Observed: {facts.recorded_holder_evidence}" if facts.recorded_holder_evidence else ""
    if verdict.allowed is True:
        return Check(
            name=CHECK_LEASE,
            ok=True,
            detail=f"{verdict.reason}{store}",
        )
    if verdict.allowed is None:
        return Check(
            name=CHECK_LEASE,
            ok=None,
            detail=f"{verdict.reason}{store}",
            hint=(
                f"look at {verdict.holder!r} before moving {agent} off {where}: is that "
                "host running this agent right now? A row naming another holder is a "
                "record of a past handover — this fleet writes the lease to the "
                "COORDINATOR's own db, and the coordinator is always the host being "
                "LEFT, so the row on THIS machine was written by the move that brought "
                "the agent here. It only becomes a split-brain when the other host is "
                "actually running the agent"
            ),
        )
    return Check(
        name=CHECK_LEASE,
        ok=False,
        detail=f"{verdict.reason}{store}.{seen}",
        hint=(
            f"this is the split-brain the lease exists to catch, and it is now backed by "
            f"an observation rather than a row: {verdict.holder!r} is running {agent}. "
            f"Settle which host owns this identity — stop it on one of them — before "
            f"relocating. Do NOT force the handover"
        ),
    )


def check_target_start(facts: TargetFacts, to_host: str, agent: str) -> Check:
    """Would the TARGET's own ``sac agents start`` accept this agent?

    Measured 2026-08-11: preflight said GO on all eleven checks, ``source_stop``
    ran, and then ``sac agents start`` on the target refused with ``sac-drift:
    spec source is 1 commit(s) BEHIND``. Nothing in the preflight had asked the
    one command the whole relocation depends on whether it would run.

    Live and about to bite again: scitex-compute-04's ``.dotfiles`` checkout —
    which is what ``~/.scitex/agent-container/agents`` is a symlink into — is five
    commits behind with 2389 modified files, so the remedy the guard prints
    (``git pull --ff-only``) aborts there. The hint says so, because a hint that
    names a command which will not run costs the reader the same trip as no hint.
    """
    drift = facts.spec_source_drift
    if drift is None:
        return Check(
            name=CHECK_TARGET_START,
            ok=None,
            detail=f"{to_host} was not asked whether its own sac would start {agent}",
            hint=(
                f"ask it: ssh {to_host} 'sac doctor' reports the spec-source drift its "
                "start command gates on. An older sac there may not carry the symbol the "
                "probe reads, in which case upgrade it — every remote step of a "
                "relocation is a sac call on that host anyway"
            ),
        )
    repo = drift.repo or "<the target's spec-source repo>"
    dirty = "" if drift.dirty is None else f", {drift.dirty} modified file(s)"
    if drift.state not in STALE_STATES:
        return Check(
            name=CHECK_TARGET_START,
            ok=True,
            detail=(
                f"{to_host}'s sac reports its spec source {drift.state}"
                f"{f' vs {drift.upstream}' if drift.upstream else ''}{dirty}; "
                "the start-time drift guard would not refuse"
            ),
        )
    return Check(
        name=CHECK_TARGET_START,
        ok=False,
        detail=(
            f"{to_host}'s sac would REFUSE to start {agent}: its spec source is "
            f"{drift.state.upper()} ({drift.behind} behind / {drift.ahead} ahead of "
            f"{drift.upstream or 'upstream'}){dirty} in {repo}"
        ),
        hint=(
            f"fix it ON {to_host} before relocating, not after: git -C {repo} pull "
            "--ff-only"
            + (
                f" — but that repo has {drift.dirty} modified file(s) and --ff-only "
                "aborts on a dirty tree, so commit or stash them there first"
                if drift.dirty
                else ""
            )
            + ". The named override is --allow-stale-spec / SAC_ALLOW_STALE_SPEC=1, "
            "which starts the agent from a spec that may be out of date; prefer the "
            "pull. This refusal happens at boot on the target, which is AFTER the "
            "agent has been stopped on the source — that is why it is asked here"
        ),
    )
