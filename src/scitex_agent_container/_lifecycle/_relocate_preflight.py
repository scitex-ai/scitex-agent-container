"""What must be true before a relocation touches anything — the whole answer, once.

This module is the entry point and nothing else: it runs every check against the
observations it is given and returns the full report. The observable SHAPES live
in :mod:`_relocate_preflight_facts` and the eleven predicates in
:mod:`_relocate_checks`; both are re-exported here, so a caller keeps importing
``TargetFacts`` / ``Check`` / ``CHECK_*`` from this one place.

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

TEN CHECKS ARE ABOUT THE TARGET AND TWO ARE ABOUT THE SOURCE. The odd ones out
are ``source_work_committed`` — whether the machine being LEFT still holds
uncommitted or unpushed work, because a relocation carries the spec and the
transcript and nothing else — and ``session_resolvable``, whether the
conversation to resume can be NAMED. Their facts come in separately
(:class:`SourceFacts`), gathered locally rather than over ssh, so a target probe
can never accidentally fill a field about the source.

``session_resolvable`` IS HERE RATHER THAN IN THE PHASES FOR ONE REASON: the
phase that needs the answer (TARGET_STANDBY) runs after the agent has been
stopped. Measured 2026-08-12, ten agents on ywata-note-win passed all eleven
checks and could not complete, every one of them holding more than one
transcript. A gate that passes on an agent that cannot proceed is the bug, not
merely a missing convenience.
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
    CHECK_SAC_PRESENT,
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
    check_sac_present,
    check_schema,
    check_session_resolvable,
    check_source_work,
)
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
    PreflightReport,
    SourceFacts,
    TargetFacts,
)

__all__ = [
    "CHECK_BINDS",
    "CHECK_CARD_STORE",
    "CHECK_CARD_STORE_DSN",
    "CHECK_GROUPS",
    "CHECK_WORKDIR",
    "UNKNOWN_BLOCKS_RELOCATION",
    "CHECK_CREDENTIALS",
    "CHECK_HUB_FROM_TARGET",
    "CHECK_IMAGE",
    "CHECK_PORTS",
    "CHECK_REACHABLE",
    "CHECK_RUNTIME",
    "CHECK_SAC_PRESENT",
    "CHECK_SCHEMA",
    "CHECK_SESSION",
    "CHECK_SOURCE_WORK",
    "Check",
    "PreflightReport",
    "SourceFacts",
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
    workdir: str = "",
    declared_groups: tuple[str, ...] = (),
) -> PreflightReport:
    """Evaluate every check against observed facts. Touches nothing.

    Returns the full report rather than the first failure, so one run surfaces
    every problem.

    ``source_facts`` defaults to nothing observed, which reports the source-work
    check as UNKNOWN and therefore refuses. That default is deliberate: a caller
    that has not looked at the source has not established the move is safe, and
    inheriting a pass for a check it never ran is the shape of every bug this
    module was written about.

    ``workdir`` and ``declared_groups`` are the spec's own claims, passed in
    rather than dug out of a spec here: this module evaluates facts and does not
    parse yaml. ``workdir`` also gives the binds check the context it needs to
    tell the agent's own material from the host's — without it every missing path
    reads as "unclassified", which is honest but far less useful.

    NO CHECK IS SKIPPED BECAUSE AN EARLIER ONE FAILED. All fifteen run, always.
    An unreachable target makes most of them UNKNOWN — each carrying the same
    probe error, which is how :mod:`_relocate_plan` recognises them as one root
    cause rather than reporting twelve independent problems. Stopping early would
    cost the operator a round trip to another machine per problem, and nine
    agents are queued.
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
        check_source_work(source_facts or SourceFacts(), from_host),
        check_session_resolvable(source_facts or SourceFacts(), agent),
    )
    return PreflightReport(agent=agent, to_host=to_host, checks=checks)
