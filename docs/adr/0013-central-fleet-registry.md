# ADR-0013 — Central, propagating fleet registry + lead-listen inbox

## Status

Proposed (2026-05-27). Operator review required before any implementation
work starts.

## Context

Two foundations are already in place:

- **ADR-0010** locked the per-host agent registry (`state.db` /
  `~/.scitex/agent-container/runtime/registry`): every `sac agents start`
  records `{name, host, bound_port, started_at, remote, spawned_by}` via
  the core start path, and the family-tree / cross-host mesh story
  depends on those rows being present.
- **PR #207** (`sac fleet sync`) added a fail-loud, read-only cross-host
  audit of `spec.yaml` + `to_home/**` across every peer in `config.yaml`,
  refusing to auto-merge on disagreement.

What is still missing is a single live answer to *"what is the fleet
doing right now?"*:

1. **Per-host registries don't compose.** Each host knows what *it*
   started. `sac fleet sync` audits specs (the definition), not
   instances (the running state). Asking "is `proj-scitex-clew`
   currently up on Spartan?" from the lead laptop today requires either
   an ssh fanout per call or trusting a stale view.
2. **The agent→lead push direction is missing.** Lead→agent A2A works
   because every agent runs `sac listen`. The lead does not — it is
   only a sender. Reverse-tunnel slices (e.g. cross-node push reverse
   tunnel) have papered over the gap one case at a time, but there is
   no general path for an agent to push a status update, an
   escalation, or a registry delta back to the lead.
3. **Liveness is undefined.** A crashed agent's registry row lingers
   until something runs `remove`. There is no TTL, no heartbeat-based
   eviction, no "this host hasn't reported in 5min, mark its agents
   stale."
4. **`sac fleet status` does not exist.** Operators infer the live
   fleet by reading individual `sac agents status` outputs per host
   and mentally union-ing them. This is the work `sac fleet sync`
   does for *specs*, applied to *running instances*.
5. **The fleet UI is not source-of-truth.** A viewer renders
   whatever the fleet reports; it cannot itself be the registry without
   collapsing UI and storage into the same layer (rejected — see
   Alternatives).

These four gaps are coupled: a central registry that does not have a
bidirectional push channel becomes a lead-side pull-fanout (today's
model); a push channel without a central registry has nowhere to push
to.

## Decision

Land a central, propagating fleet registry plus the minimum transport
surface to make it bidirectional. Five components, taken together:

### 1. Propagating registry (child → parent → root, with downward sync)

Every agent registry row propagates **up the spawn DAG** (ADR-0010's
family tree): a child writes locally, then forwards the delta to its
parent's host registry; the parent forwards to its parent; up to the
fleet root (lead). The root then **syncs back down** to every node,
so every host eventually holds the full fleet view.

The propagation surface piggybacks on the spawn lineage already
recorded by ADR-0010 (`spawned_by`) — no new topology config.

### 2. Lead runs `sac listen` as its inbox

The lead host runs the same `sac listen` HTTP control plane that every
agent runs. Effect: the agent→lead push direction uses the *identical*
A2A `/v1/turn`-style surface as lead→agent, with no special-case
reverse-tunnel code path. This closes the "agent cannot push to lead"
gap structurally.

Listening on the lead is the prerequisite that makes the propagating
registry (#1) deliverable to a single root.

### 3. ssh-mesh bidirectional routing

Routing reuses the existing ssh mesh — ControlMaster multiplexing from
PR #218 (`fix/ssh-control-master-multiplex`), `build_ssh_argv` /
`env_preamble` from PR #207's fan-out — but flipped: agent→lead uses
the same connection-reuse and config-resolution path as lead→agent.
No new transport. No reverse tunnel per agent.

### 4. `sac fleet status` CLI verb

A new read-only verb that reads the locally-cached *propagated*
registry and prints the unified live fleet view (per-host, per-agent,
with bound port, started_at, last heartbeat, spawned_by). Output shape
mirrors `sac fleet sync`'s text/JSON envelope (loudness contract,
exit-code semantics) for consistency.

Because the registry is propagated (#1), `sac fleet status` is a
**local** read — no ssh fanout per invocation, no O(N) cost.

### 5. Liveness eviction

Heartbeat-driven TTL: each running agent refreshes its registry row
on a fixed interval (reusing the heartbeat mechanism already wired
for cred-refresh and CREDS-PHASE1 picker). The root evicts rows that
miss N consecutive heartbeats and propagates the eviction downward
like any other delta. Eviction is server-driven, never caller-driven
— a dead agent does not stay alive because nobody happens to call
`sac agents status`.

### Non-goal: the fleet UI stays UI-only

A fleet UI is a **consumer** of the central fleet registry, not its
owner. The registry is sac-internal; the UI reads it (via a `sac
fleet status --json` or equivalent surface) and renders. Mixing UI
and source-of-truth into the UI layer was considered and rejected — see
Alternatives.

## Consequences

### Positive

- **Single live fleet view, available on every host.** Any operator
  shell on any host (lead, Spartan, a peer) can answer "what is
  running fleet-wide?" with a local read.
- **Agent→lead push is now first-class A2A.** Escalations, status
  pushes, and registry deltas all travel the same surface as
  lead→agent; no per-feature reverse-tunnel code.
- **Dead rows go away automatically.** Liveness eviction removes the
  "did we restart that or not?" ambiguity that today bites every
  multi-day fleet run.
- **`sac fleet sync` (PR #207) and `sac fleet status` form a clean
  pair**: sync audits the definition, status reports the running
  instance — same loudness contract, same JSON envelope shape.
- **The UI gets a stable upstream.** UI work decouples from registry
  work because the registry has a defined `--json` surface.

### Negative / things to watch

- **Propagation is a new surface.** Failure modes: partial partitions
  (parent reachable, grandparent not), late-arriving deltas after
  eviction, clock skew on `started_at` / heartbeat timestamps. The
  implementation must define convergence behavior for each, and the
  loudness contract from ADR-0011 / PR #207 (fail loud, never silent
  fallback) applies here too — a node that cannot reach its parent
  must surface that, not paper over it.
- **Lead `sac listen` becomes a fleet dependency.** If the lead's
  listen is down, the agent→lead push direction degrades. The
  per-host local registry must remain usable in lead-down mode
  (degraded: no fleet-wide view, but local operations continue).
- **Heartbeat traffic.** N agents × heartbeat interval × propagation
  fan-out = registry write load. Eviction TTL and heartbeat interval
  must be tuned together to avoid flapping evictions on a slow
  network.
- **ACL implications (ADR-0010).** Registry propagation must respect
  the server-managed ACL: a child's registry delta is forwarded by
  the parent only if the parent's ACL allows it, and the root applies
  the lead's view. No new ACL primitives — reuse ADR-0010's.

## Alternatives considered

- **Pull-only fleet view (status quo + better fanout).** Have
  `sac fleet status` ssh into every peer and union the local
  registries on each call. Rejected: O(N) per status invocation, no
  realtime view, scales badly when an operator has 10+ hosts, and
  duplicates the fanout logic PR #207 already has for specs.

- **Centralized registry only on lead, no propagation.** Single
  source-of-truth on the lead; every host queries the lead. Rejected:
  single point of failure, every other host is blind when the lead's
  listen is down, and the lead laptop is exactly the machine most
  likely to be offline (closed lid, traveling).

- **Make the fleet UI the central registry.** The UI already aggregates a
  fleet view for display; tempting to let it own storage too.
  Rejected: collapses UI and source-of-truth into one process, makes
  sac depend on a UI to know what is running, and forces any future
  non-UI consumer (a CLI, a CI check, a different UI) to talk to
  a UI server. The UI stays a thin consumer of the sac surface.

- **Per-feature reverse tunnels for agent→lead push.** Continue the
  current pattern: ship a targeted reverse-tunnel slice each time a
  new agent→lead push case appears. Rejected: each slice adds a
  parallel transport path; the surface area diverges from the
  lead→agent A2A path and any future cross-feature interaction
  (e.g. an escalation that also carries a registry delta) requires
  bridging the two. Running `sac listen` on the lead collapses them.

- **Polling-only liveness (no heartbeat).** Periodically run `kill
  -0` on recorded pids and prune dead ones. Rejected: works for
  same-host pids but not cross-host (the pid is meaningless on a
  remote machine), and a hung-but-alive agent (pid up, doing
  nothing) would not be evicted. Heartbeat is the only check that
  reflects *behavior*, not just process existence.

## References

- ADR-0008 — sac node transport boundary (the A2A surface this ADR
  extends symmetrically to the lead).
- ADR-0010 — agent-spawn family tree + server-managed ACL (provides
  `spawned_by` lineage on which propagation rides, and the ACL that
  bounds it).
- ADR-0012 — orchestration tooling develops locally (the lead's local
  host is also where `sac listen` will run, per this ADR).
- PR #207 — `sac fleet sync` cross-host spec audit (sibling verb;
  this ADR proposes `sac fleet status` with the same loudness
  contract).
- PR #218 — ssh ControlMaster multiplex (the connection reuse that
  the bidirectional mesh inherits).
- Lead design doc (working notes): `~/proj/scitex-lead/GITIGNORED/
  FUTURE/central-fleet-registry.md` on the lead host. This ADR was
  written from the operator's inline summary on 2026-05-27; the
  working notes are the authoritative source and should be
  reconciled with this ADR's text during implementation review.

## Sequencing notes

- Operator must review and accept this ADR (Status → Accepted) before
  any implementation work starts. No code changes in this PR.
- Implementation lands in a sequence: (a) lead `sac listen`, (b)
  bidirectional ssh-mesh routing, (c) propagation primitives, (d)
  `sac fleet status` verb, (e) liveness eviction. Each step is a
  separate PR with its own tests, following the per-work-item PR
  rule.
