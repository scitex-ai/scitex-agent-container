# 0010 — Agent-spawn family-tree + server-managed ACL

- Status: Accepted (design locked by operator 2026-05-23/24; staged implementation)
- Deciders: ywatanabe (operator), lead orchestrator
- Supersedes the ad-hoc per-agent `grant_send` stopgap for cross-agent comms.

## Context

SAC agents need to spawn other SAC agents (e.g. `proj-paper-scitex-clew`
spawning per-capsule execution agents), across hosts, without escalating
privilege. Two failures motivated this:

1. **Remote-registry gap (2026-05-23):** remote-dispatched agents wrote no
   local `state.db` row, so `sac agents status` / `sac peer post-turn` could
   not resolve them ("port: auto, no bound port recorded"). The lead could not
   reach `clew` on Spartan and had to relay through the (resting) operator.
2. **Escalation-by-spawn risk:** a low-privilege agent could birth a
   high-privilege child (or ask a sibling/parent to do so) to escape its own
   ACL bounds.

The driving question (from clew): can a SAC agent spawn SAC agents, and can it
use the host SLURM? Yes to both — but only through a structure that records
every spawn and clamps every child's privileges.

## Decision

### Three spawn/dispatch mechanisms (all must exist)

1. **ssh-localhost** — agent runs `ssh localhost sac agents start <child>`;
   executes on the BARE host, booting a sibling container at host level (avoids
   apptainer-in-apptainer nesting, blocked on HPC). **The self-spawn key is NOT
   provisioned into containers** (see ACL §"loophole closure") so this path
   cannot be used to bypass the server — it remains a host-operator tool, not an
   agent self-spawn path.
2. **SLURM** — agents inside a SLURM reservation submit jobs via `sbatch`.
   Requires the SLURM client + munge auth reachable from inside the container:
   munge in the SIF + host-binds of `/apps/slurm` + `/run/munge` + `slurm.conf`
   in the agent spec. (Implemented + verified on Spartan 2026-05-24:
   in-container `sbatch` → slurm 25.05.3, munge OK.)
3. **sac-server-mediated (cleanest, canonical)** — agent calls the host-side
   `sac listen` control plane `POST /agents`; the server (on the bare host)
   starts the child on the agent's behalf. This is the ONLY sanctioned
   agent-driven spawn path, so ACL is always enforced.

### Two universal control rules

- **A. Definition file governs.** Which mechanism, where (host/node), and
  whether a start is allowed — all decided by the child's `spec.yaml`
  (dir-as-SSoT). No ad-hoc placement logic.
- **B. Automatic recording in the start codepath.** Recording is an intrinsic,
  unskippable side-effect of `sac agents start` — NOT a separate call the
  caller must remember. Every start path (local, remote dispatch, server)
  flows through the same core start function, which always writes the registry
  row `{name, host, BOUND a2a_port, started_at, remote, spawned_by}` before
  returning. Add a new spawn mechanism → recording comes for free.
  *(Implemented: PR #189, 2026-05-24 — schema cols `bound_port`/`remote`/
  `spawned_by`; remote `status`/`peer post-turn` now resolve via the
  cross-host `instances` row.)*

### Emergent family-tree (spawn DAG)

Because every parent automatically records every child with lineage
(`spawned_by`), the registry accumulates a complete parent→child spawn DAG for
free — directly visualizable as an agent family tree. Falls out of rules B + C.

### Server-managed ACL

ACL (who may message/drive/spawn whom) is managed centrally at the `sac listen`
server, NOT via scattered ad-hoc `grant_send` rows. The server is the single
point that knows the fleet topology + grants.

**Operator-locked ACL rules (2026-05-24):**

- **lead → ALL agents = always allowed.** The lead is the human interface and
  must be able to coordinate the whole fleet. (As of 2026-05-24 lead→agent is
  *denied* by default — this rule is required and is the first to implement.)
- **Depth: two generations** to start.
- **Worked example:** `proj-paper-scitex-clew` = FULL ACL; its children
  (capsule executions) spawn with NARROWED ACL from clew.
- **child.acl ⊆ parent.acl by default.** Exceptions are YAML-defined, AND a
  parent may grant an exception to a child ONLY IF the parent itself holds that
  exception (otherwise it is a bypass). Server-enforced.
- **Loophole closure:** spawn must go through the server (#3) only —
  `起動経路 = 記録経路 = ACL経路` collapsed to one path. The ssh-localhost
  self-spawn key is not provisioned into containers, so an agent cannot bypass
  the server. Host-loop escalation is blocked because the server clamps every
  spawn to the *requester's* ACL regardless of target host. Proxy/collusion
  (asking a parent/sibling to spawn for you) is blocked by the same
  clamp-to-requester: the spawn is bounded by the requester's privileges, not
  the spec's wish.
- **Relaxed mode** is YAML-configurable (extend `spec.apptainer.relaxed` to
  ACL) but Rule "child ⊆ parent" is invariant — only operator/cli-spawned
  agents (parent = full ACL) can be broad.
- **Zero-trust:** enforced in server/code, never by prompt convention.

### Cross-host mesh

Registry + dispatch + ACL span hosts: an agent on host X can be started,
recorded, reached, and ACL-checked from host Y. The bound-port + host recorded
by rule B make cross-host `status`/`post-turn` resolvable.

## Current implementation state (2026-05-24)

| Phase | Scope | Status |
|---|---|---|
| 1 | auto-record in core start + schema (`bound_port`/`remote`/`spawned_by`); remote resolve | **MERGED (PR #189)** |
| 2 | mechanism #3 (server-mediated spawn via `sac listen POST /agents`, routed through core start; `spawned_by` = requester) | pending (a prior attempt died on a transient 529 throttle) |
| 3 | server-managed ACL: a `sac acl grant/revoke/list` CLI (no such CLI exists today — only an internal `grant_send` writing `state.db`), lead-privileged rule, child⊆parent enforcement, ssh-localhost self-spawn block, YAML exceptions | pending |
| — | family-tree DAG visualization | pending (schema fields ready) |
| #2 prereq | SLURM client in SIF + munge/host-binds | **DONE + verified on Spartan** |

`clew`'s SLURM-route specifics are coordinated by the operator with clew
directly (out of lead scope).

## Consequences

- A fully-trackable, loophole-free, cross-host agent fleet whose spawn lineage
  is a visualizable family-tree graph.
- Each component does its bounded job (start records; spec governs; server owns
  ACL); composed, they yield the invariant guarantees above.
- The remote-registry gap (the lead could not reach `clew`) is structurally
  eliminated once Phase 1 is in use (done).
- Escalation-by-spawn is structurally blocked by monotonic ACL narrowing
  (server-enforced), not by prompt convention.

## References

- Lead design doc (working notes): `~/.scitex/todo/docs/sac-agent-spawn-design.md`
- Driving conversation: operator Telegram 4318-4340 (design), 4428 (ACL detail)
- Related: ADR 0008 (node transport boundary), 0006 (to-home materialization)
