# ADR-0012: Orchestration tooling develops locally; library packages on Spartan

## Status
Accepted (2026-05-27)

## Context
The `proj-scitex-*` development agents run on Spartan (a SLURM compute
reservation, e.g. bm001) so heavy work — pytest matrices, builds — runs
on compute rather than the lead laptop.

`scitex-agent-container` (sac) is different in kind: it is the tool that
**launches and drives** those agents (`sac agents start`, `sac host exec`,
the A2A `/v1/turn` surface). Developing and testing sac therefore means
launching agents and interacting with them. Doing that from inside a
Spartan apptainer container would mean an agent launching agents **inside
its own container** — nested containers, which are fragile and hit the
sandbox bind/permission limits that already constrain in-container agents.

The operator raised a consistency concern: if every other `proj-scitex-*`
agent lives on Spartan, why is this one local? Inconsistency is a cost
(it changes where one edits, and must be justified).

## Decision
sac (the fleet-orchestration tool) is developed on the **local lead host**
(the machine the fleet originates from). All other library / compute
packages (`scitex-io`, `scitex-gen`, `scitex-stats`, ...) are developed on
**Spartan**.

Rule: **orchestration tooling → local lead host; library / compute
packages → Spartan.**

## Consequences
- sac development can launch and drive real agents to test itself without
  container nesting.
- The sync workflow is **unchanged**: GitHub `develop` remains the only
  sync substrate (local edits push to `develop`; Spartan pulls). Only the
  *edit location* differs, not the sync model — so consistency of workflow
  is preserved.
- This is one **principled** exception to "all `proj-*` agents on Spartan,"
  justified by the orchestration-at-origin requirement, not an ad-hoc
  placement. A second tool that orchestrates the fleet would follow the
  same rule.

## Notes
Surfaced 2026-05-26/27 while diagnosing a Spartan home-quota wedge; the
operator asked for this decision to be recorded rather than left implicit.
