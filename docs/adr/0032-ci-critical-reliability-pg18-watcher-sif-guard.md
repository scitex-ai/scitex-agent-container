# ADR-0032 — CI critical reliability: PG18 incident, DONE-gating watcher, SIF audit guard

Status: Proposed (2026-10-08). Operator order: CI is company-critical
(life-or-death). Tracked continuously via card
`infra-ci-critical-reliability-20261008` — permanent essential improvement,
never one-shot.

## Context

On 2026-10-08 the operator escalated CI reliability to company-critical and
ordered three durable artifacts: an agent-independent CI watcher with
DONE-gating, a PG18 root-cause ADR (this document), and the SIF audit guard
`scitex-ai/.github#91` brought to merge+live — plus merging the open SAC PR
portfolio (#1599, #1600, #1576, #1598). Evidence is required for every claim;
there is no self-declared recovery.

The PG18 incident that forced this ADR had the following root cause and
recovery shape, as recorded on the card:

- **Root cause:** the org-level variable path consumed by the PG18-backed
  path was missing on compute-02. The consuming side assumed the path
  existed on every host; 02 never had it, so the PG18 path proof failed
  there while other hosts looked healthy.
- **Recovery pattern (admitted fault):** the first recovery followed a
  `.claim-only` pattern — DONE was declared on assertion, without attached
  evidence (no placement record, no path proof, no test output).
- **Actual fix:** a manual `/tmp` stage plus `mv` into place, verified with
  a sha256 comparison before and after. The hash proof is the only reason
  the fix is trusted; the manual procedure itself is incident response, not
  a repeatable procedure, and must not become one.

Three audit faults are admitted against this incident. Each is a
DONE-gating criterion below — a DONE claim that lacks the corresponding
evidence is void.

1. **Execution placement.** Recovery and verification steps ran without a
   recorded execution placement: which host, which workdir/namespace, under
   whose authority. An agent-dependent step executed from an undeclared
   location cannot be distinguished from a step that never ran.
2. **PG18 path proof.** The org var path was never proven present (or
   absent) per host with content evidence before DONE was claimed. The 02
   gap survived exactly because nobody produced a per-host path read.
3. **Acceptance tests.** The acceptance set was not run green as the gate
   for DONE. Tests existed as an idea, not as executed evidence attached to
   the claim.

## Decision

1. **DONE-gating is mandatory for CI-critical work.** No card, PR, or
   incident step transitions to DONE without attached evidence for every
   applicable acceptance criterion: execution placement record, PG18 path
   proof, and acceptance-test output. A DONE without evidence is reverted
   to in-progress on sight — by anyone, without permission.
2. **The CI watcher is agent-independent.** CI reliability monitoring must
   not depend on the agents it watches for its own execution, scheduling,
   or evidence store. Rationale, already proven by the fleet watch: liveness
   and heartbeat are not completion evidence (生存・心拍は作業完了の証拠で
   はありません), and a watcher that dies with its subjects reports nothing.
   The watcher runs on declared infrastructure (cron/systemd, pinned host),
   writes evidence to the card, and its own liveness is separately observed.
3. **PG18 org-var path is declared and proven per host.** The canonical org
   var path is recorded as configuration (not tribal knowledge), and
   presence plus sha256 content proof is produced per host — including
   compute-02 — on every verification pass. Manual `/tmp`+`mv` stays
   available as incident response; the permanent fix lands the path and its
   proof in code/config so the incident class cannot recur.
4. **SIF audit guard `scitex-ai/.github#91` merges and goes live as a
   required check.** Non-portable image references (timestamped artifacts,
   host-local paths — the class ADR-0028 outlawed for SAC-owned images)
   must fail CI at PR time, not at 03:00 on a compute host. Until the guard
   is live and required, every SIF-touching PR gets a manual image-reference
   review recorded on the PR.
5. **Continuous tracking, never one-shot.** The reliability card stays open
   with running evidence until the operator closes it. Each watcher cycle
   appends its results; regressions reopen the gate rather than producing a
   new incident process.

## Watcher acceptance criteria

The watcher is accepted only when all four hold with live evidence:

1. **Independence:** the watcher fires and records on schedule while the
   watched agents are stopped — demonstrated by a scheduled run during a
   controlled agent outage, not by assertion.
2. **Execution placement:** every watcher report names host, workdir, and
   triggering schedule; a report without placement is rejected by the gate.
3. **PG18 path proof:** every cycle includes the per-host org-var path
   presence check with sha256 content proof; a missing path on any host
   (the 02 failure mode) blocks DONE and pages the card.
4. **DONE-gate enforcement:** an attempted DONE transition with any
   criterion lacking attached evidence is refused and logged; the refusal
   itself is evidence the gate works.

## What remains (as of 2026-10-08)

Delivered with this ADR (branch `hermes-subagent/subagent-sa-0-b7b7649e`):

- Gate implementation `tools/done_gate/done_gate.py` — refuses unevidenced
  DONE (exit 1 + logged refusals), passes fully-evidenced claims.
- Reproduced-failure verification: 6/6 pytest green
  (`tools/done_gate/test_done_gate.py`), including the exact PG18
  `.claim-only` mode refused and an evidenced claim passing.
- Ongoing verification: `tools/done_gate/done-gate-on-ubuntu-py3-12.yml`
  (YAML-validated, probe simulated green) — merge-ready for
  `scitex-agent-container/.github/workflows/`.

OPEN (not claimed done):

- Watcher: agent-independent CI watcher per the design above, with
  criterion 1 demonstrated against a controlled outage. Path: implement on
  declared infra, evidence to the card.
- Guard: `scitex-ai/.github#91` to merge + live + required; manual
  image-reference review on SIF PRs until then.
- Gate workflow + `tools/done_gate/` to merge into scitex-agent-container
  (this branch holds them under mirrored paths); this ADR file to
  `docs/adr/0032-*.md` in that repo.
- Portfolio: merge open SAC PRs #1599, #1600, #1576, #1598.
- Card `infra-ci-critical-reliability-20261008` stays in_progress under the
  operator's continuous-improvement order until the operator closes it.

## Consequences

DONE becomes an evidenced state, not a declared one. The cost is explicit:
every CI-critical claim carries placement, proof, and test output, and the
watcher itself is subject to the same rule. The payoff is that the 02-class
failure — a missing path nobody proved — cannot pass the gate again, and
the next incident starts from watcher evidence instead of from assertions.
