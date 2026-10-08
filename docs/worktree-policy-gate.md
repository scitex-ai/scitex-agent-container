# Harness-neutral worktree policy gate

SAC owns worktree policy. It decides every allow/deny in-process
(`_lifecycle/_worktree_policy_engine.py`) from the selected manifest plus
real Git inspection, and records the manifest/projection identity on the
launch. Resolution order: `SAC_WORKTREE_POLICY_PATH`, else the per-host
state copy at `~/.scitex/agent-container/worktree-policy/` when present,
else the bundled `_baseline_assets/worktree_policy/worktree-policy.json`.
No operator-side executable is required. Operators who keep their own policy
executable can still pass its path as `cli_path`; the engine schema and this
page document the contract it must honor.

## Launch behavior

Before a write-capable `kind: Agent` task starts, SAC:

1. validates its generated projections are current;
2. inspects the authored `spec.workdir`;
3. resolves a stable, agent-owned linked worktree and authorizes
   any provisioning command against the engine;
4. changes the in-memory runtime workdir to that linked worktree; and
5. authorizes edit context there immediately before launch.

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

Any timeout, denial, stale generated projection, invalid hash, identity
change between checks, dirty/conflicting ownership, failed provisioning,
or unreadable manifest refuses launch. There is no permissive
fallback.

## Recovering an absent owned checkout

`sac agents restore-worktree NAME --expected-tip FULL_COMMIT_ID` is an explicit
dry-run operation on the agent's declared owning host. It reads the existing
canonical runtime ownership record and asks the policy engine's
`check-owned-restore` verb to approve only that recorded missing path and
retained existing branch. The authored workdir must name either the recorded
primary repository or the exact recorded checkout. The command emits JSON with
the owner digest, primary branch/HEAD/status, retained full tip, worktree
registration digest, policy/projection hashes, exact Git argv and receipt hash.

After reviewing that receipt, apply the same operation with
`--apply --receipt-sha256 RECEIPT_SHA256`. SAC refuses changed receipts,
serializes official restores, repeats neutral approval immediately before
normal `git worktree add PATH EXISTING_BRANCH`, and verifies the result. It
preserves primary branch/content/HEAD, ownership bytes, retained branch tip
and history. It does not use force, create a branch, reset, delete, adopt a
checkout, or launch an agent. A postverification failure preserves the
checkout for inspection.

This bounded capability requires an explicitly enabled neutral manifest and
fresh generated projections. General `check-shell` and launch gates retain
their existing rules. If the primary's current branch is unsuitable for the
normal launch gate, author the spec workdir as the matching restored owned
checkout, then perform the ordinary launch preflight. Restoring committed
branch content cannot recover absent uncommitted files.

The SAC suite uses a subprocess protocol fixture for hermetic mechanics tests
of the external-CLI opt-in path. New default-path coverage exercises the
in-process engine directly with real Git repositories
(`test__worktree_policy_engine.py`).
To run its additional cross-repository contract against an actual external
policy executable, set `SCITEX_WORKTREE_POLICY_TEST_CLI` to its reviewed
path and run
`tests/scitex_agent_container/_lifecycle/test__worktree_restore.py`; the neutral
repository independently tests all approval and refusal shapes with real Git.

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
workdirs and the engine-approved context.

`kind: AgentProxy` is outside this gate because it launches no mutation-capable
agent harness.

## Capability boundaries and drift

| Surface | Responsibility |
| --- | --- |
| Manifest + engine | Sole executable decision contract and policy identity. |
| SAC lifecycle | Resolve ownership, invoke the engine, fail closed, and persist evidence. |
| External CLI opt-in | Transport only when configured; it must honor the engine schema. |
| MCP | Transport only; it must call the engine rather than reproduce decisions. |
| Hooks | Harness event adapters, never cross-harness authority. |
| Skills/prompts/docs | Generated guidance or hash-validated projections only; never authorization. |
| Commands | User-facing wrappers around executable behavior, never policy. |

SAC ships no worktree-policy skill or launch prompt containing a second copy of
the rules. Host AGENTS/CLAUDE/HERMES explanatory projections are accepted only
when the engine's `check-projections` result is current and hash-valid. Editing
prose cannot change a launch decision. Consult the bundled manifest and the
engine module for the current rules rather than this document.

The external-CLI opt-in boundary is tested with the executable fixture under
`tests/scitex_agent_container/_lifecycle/_fixtures/worktree-policy/`. It models
the CLI protocol and real Git worktree operations, not a second policy engine.
The engine itself is tested in-process with real Git repositories.
