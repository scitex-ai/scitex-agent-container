# Harness-neutral worktree policy gate

SAC does not own worktree policy. It delegates every decision to the
operator-owned `~/.dotfiles/src/.bin/scitex-worktree-policy` executable and
records the returned manifest/projection identity. The executable schema and
runtime configuration are authoritative; this page documents only SAC's
tested lifecycle adapter.

## Launch behavior

Before a write-capable `kind: Agent` task starts, SAC:

1. asks the CLI to validate its generated projections;
2. inspects the authored `spec.workdir` through the CLI;
3. resolves a stable, agent-owned linked worktree and asks the CLI to authorize
   any provisioning command;
4. changes the in-memory runtime workdir to that linked worktree; and
5. asks the CLI to authorize edit context there immediately before launch.

The linked path and branch are deterministic for the agent identity. Ownership
is recorded at
`~/.scitex/agent-container/runtime/<agent>/worktree-owner.json`, including the
spec, session/resume, and incarnation identity. A subsequent incarnation of
the same agent reuses the owned worktree, including unfinished changes, and
refreshes the runtime identity in that record.

SAC never copies, deletes, commits, or resets existing work. It refuses a dirty
authority checkout, an unowned dirty linked worktree, a branch/owner mismatch,
or a target whose Git identity changed. This makes migration explicit: preserve
or move existing authority-checkout changes first, then rerun
`sac agents explain <name>` or `sac agents start <name> --dry-run`. A clean
authority checkout needs no manual migration; the real start provisions the
planned worktree.

Dry-run and explain execute the same projection checks and render the authored
workdir, planned resolved workdir, branch, action, and hashes without creating
the worktree. A dry-run plan is evidence, not reusable permission. Production
launch repeats validation and obtains edit authorization against the created or
reused worktree. A forced restart is checked before the old process is stopped;
a supervisor restart repeats the gate.

Any missing executable, timeout, denial, stale generated projection, malformed
response, invalid hash, identity change between checks, dirty/conflicting
ownership, or failed provisioning refuses launch. There is no permissive
fallback.

## Runtime and container compatibility

Resolution happens on the host. The container does not need dotfiles or a
harness-specific policy hook. SAC passes the resolved host path through the
existing runtime adapter, so the container's normal bind plan must cover it.
Whole-home binds used by the current Hub/application specs naturally cover
their repository-local `.worktrees` paths. A narrower mount that does not cover
the resolved path remains visibly invalid in `agents explain`; SAC does not
guess a container mapping.

The accepted proof is persisted on the incarnation as `policy_sha256` and
`projection_sha256`, and in
`compiled_spec_json.launch_artifacts.worktree_policy` with authored/resolved
workdirs and the CLI-approved context.

`kind: AgentProxy` is outside this gate because it launches no mutation-capable
agent harness.

## Capability boundaries and drift

| Surface | Responsibility |
| --- | --- |
| CLI + manifest | Sole executable decision contract and policy identity. |
| SAC lifecycle | Resolve ownership, invoke the CLI, fail closed, and persist evidence. |
| MCP | Transport only; it must call the CLI rather than reproduce decisions. |
| Hooks | Harness event adapters, never cross-harness authority. |
| Skills/prompts/docs | Generated guidance or hash-validated projections only; never authorization. |
| Commands | User-facing wrappers around executable behavior, never policy. |

SAC ships no worktree-policy skill or launch prompt containing a second copy of
the rules. Host AGENTS/CLAUDE/HERMES explanatory projections are accepted only
when the CLI's `check-projections` result is current and hash-valid. Editing
prose cannot change a launch decision. Consult the CLI/manifest for the current
rules rather than this document.

The subprocess boundary is tested with the executable fixture under
`tests/scitex_agent_container/_lifecycle/_fixtures/worktree-policy/`. It models
the CLI protocol and real Git worktree operations, not a second policy engine.
