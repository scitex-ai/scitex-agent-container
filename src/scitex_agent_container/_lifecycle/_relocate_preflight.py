"""What must be true before a relocation touches anything — the whole answer, once.

This module is the entry point and nothing else: it runs every check against the
observations it is given and returns the full report. The observable SHAPES live
in :mod:`_relocate_preflight_facts`, the predicates learned from doing the move
BY HAND in :mod:`_relocate_checks`, and the two learned from watching it RUN in
:mod:`_relocate_checks_late`; all three are re-exported here, so a caller keeps
importing ``TargetFacts`` / ``Check`` / ``CHECK_*`` from this one place.

WHY EVERY CHECK, NOT THE FIRST FAILURE. The operator asked for a dry run, and a
dry run that stops at the first problem makes him run it N times to find N
problems. Ten of the eleven checks were learned by hitting them one at a time on
2026-08-07; that is exactly the experience this shape exists to prevent
repeating.

UNKNOWN NEVER COUNTS AS PASS. Each check is three-valued, and the aggregate
refuses on unknowns just as it refuses on failures — reported separately so the
reader can tell "this is wrong" from "I could not tell". An unknown folded into
pass is precisely how the 08-07 move reported healthy: the credential check had
no answer, and something treated that as fine.

ELEVEN CHECKS ARE ABOUT THE TARGET, TWO ABOUT THE SOURCE, ONE ABOUT NEITHER. The
source pair is ``source_work_committed`` — whether the machine being LEFT still
holds uncommitted or unpushed work, because a relocation carries the spec and the
transcript and nothing else — and ``session_resolvable``, whether the
conversation to resume can be NAMED. Their facts come in separately
(:class:`SourceFacts`), gathered locally rather than over ssh, so a target probe
can never accidentally fill a field about the source. The odd one out is
``lease_holdable``, whose facts are read from the coordinator's own state db and
from whichever host the stored row happens to name (:class:`LeaseFacts`).

THREE CHECKS ARE HERE BECAUSE THE PHASE THAT NEEDS THEM RUNS TOO LATE, and they
are one story told three times:

    session_resolvable    TARGET_STANDBY refuses without a session id, and it
                          runs after SOURCE_STOP. Measured 2026-08-12: ten
                          agents on ywata-note-win passed every check and not
                          one of them could complete.
    target_start_accepts  the target's own ``sac agents start`` refuses a spec
                          source that is BEHIND, and TARGET_STANDBY is what
                          calls it — again after SOURCE_STOP. Measured
                          2026-08-11; it cost the canary its first leg.
    lease_holdable        HANDOVER refuses a lease held by another host, and it
                          runs after SOURCE_STOP, TRANSPORT, TARGET_STANDBY and
                          HANDSHAKE. Measured 2026-08-11 on the return leg: exit
                          5, with the agent stopped and nothing running anywhere.

A gate that passes on an agent that cannot proceed is the bug, not merely a
missing convenience.
"""

from __future__ import annotations

from ._relocate_checks import (
    CHECK_BINDS,
    CHECK_CARD_STORE,
    CHECK_CREDENTIALS,
    CHECK_HUB_FROM_TARGET,
    CHECK_IMAGE,
    CHECK_PORTS,
    CHECK_REACHABLE,
    CHECK_RUNTIME,
    CHECK_SCHEMA,
    CHECK_SESSION,
    CHECK_SOURCE_WORK,
    check_binds,
    check_card_store,
    check_credentials,
    check_hub_from_target,
    check_image,
    check_ports,
    check_reachable,
    check_runtime,
    check_schema,
    check_session_resolvable,
    check_source_work,
)
from ._relocate_checks_late import (
    CHECK_LEASE,
    CHECK_TARGET_START,
    check_lease_holdable,
    check_target_start,
)
from ._relocate_checks_sac import CHECK_SAC_PRESENT, check_sac_present
from ._relocate_checks_spec import (
    CHECK_CARD_STORE_DSN,
    CHECK_GROUPS,
    CHECK_WORKDIR,
    check_card_store_dsn,
    check_target_groups,
    check_workdir,
)
from ._relocate_preflight_facts import (
    UNKNOWN_BLOCKS_RELOCATION,
    Check,
    LeaseFacts,
    PreflightReport,
    SourceFacts,
    SpecSourceDrift,
    TargetFacts,
)

__all__ = [
    "CHECK_BINDS",
    "CHECK_CARD_STORE",
    "CHECK_CARD_STORE_DSN",
    "CHECK_CREDENTIALS",
    "CHECK_GROUPS",
    "CHECK_HUB_FROM_TARGET",
    "CHECK_IMAGE",
    "CHECK_LEASE",
    "CHECK_PORTS",
    "CHECK_REACHABLE",
    "CHECK_RUNTIME",
    "CHECK_SAC_PRESENT",
    "CHECK_SCHEMA",
    "CHECK_SESSION",
    "CHECK_SOURCE_WORK",
    "CHECK_TARGET_START",
    "CHECK_WORKDIR",
    "UNKNOWN_BLOCKS_RELOCATION",
    "Check",
    "LeaseFacts",
    "PreflightReport",
    "SourceFacts",
    "SpecSourceDrift",
    "TargetFacts",
    "check_card_store_dsn",
    "check_target_groups",
    "check_workdir",
    "preflight",
]


def preflight(
    *,
    agent: str,
    to_host: str,
    facts: TargetFacts,
    runtime: str,
    required_ports: tuple[int, ...] = (),
    source_facts: SourceFacts | None = None,
    from_host: str = "",
    lease_facts: LeaseFacts | None = None,
    workdir: str = "",
    declared_groups: tuple[str, ...] = (),
) -> PreflightReport:
    """Evaluate every check against observed facts. Touches nothing.

    Returns the full report rather than the first failure, so one run surfaces
    every problem.

    ``source_facts`` and ``lease_facts`` both default to nothing observed, which
    reports their checks as UNKNOWN and therefore refuses. That default is
    deliberate: a caller that has not looked has not established the move is
    safe, and inheriting a pass for a check it never ran is the shape of every
    bug this module was written about. It is also why adding a check here can
    only ever make a previously-passing run refuse — never the reverse.

    ``workdir`` and ``declared_groups`` are the spec's own claims, passed in
    rather than dug out of a spec here: this module evaluates facts and does not
    parse yaml. ``workdir`` also gives the binds check the context it needs to
    tell the agent's own material from the host's — without it every missing path
    reads as "unclassified", which is honest but far less useful.

    NO CHECK IS SKIPPED BECAUSE AN EARLIER ONE FAILED. All seventeen run, always.
    An unreachable target makes most of them UNKNOWN — each carrying the same
    probe error, which is how :mod:`_relocate_plan` recognises them as one root
    cause rather than reporting a dozen independent problems. Stopping early
    would cost the operator a round trip to another machine per problem.
    """
    checks = (
        check_reachable(facts, to_host),
        check_image(facts, to_host),
        check_binds(facts, to_host, workdir=workdir, from_host=from_host),
        check_workdir(facts, to_host),
        check_card_store_dsn(facts),
        check_card_store(facts, to_host),
        check_credentials(facts),
        check_runtime(facts, runtime),
        check_schema(facts),
        check_ports(facts, required_ports),
        check_target_groups(facts, declared_groups, to_host),
        check_hub_from_target(facts, to_host),
        check_sac_present(facts, to_host),
        check_target_start(facts, to_host, agent),
        check_source_work(source_facts or SourceFacts(), from_host),
        check_session_resolvable(source_facts or SourceFacts(), agent),
        check_lease_holdable(lease_facts or LeaseFacts(), from_host, agent),
    )
    return PreflightReport(agent=agent, to_host=to_host, checks=checks)
