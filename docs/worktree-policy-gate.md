# Harness-neutral worktree policy gate

SAC ships its harness-neutral policy checker and default manifest in the Python
package. `sac worktree policy` runs that checker from an installed wheel without
requiring a source checkout, dotfiles, or a host helper script.

The checker is an installed private package. Lifecycle invokes its absolute
`__main__.py` path with the current Python interpreter, so changing the working
directory or `PYTHONPATH` cannot select a different SAC checkout. The CLI imports
the same checker; Git context, shell evaluation, and policy configuration each
have a separate module.

An optional host manifest lives at
`~/.scitex/agent-container/worktree-policy/worktree-policy.json`, with its
projections in the adjacent `generated/` directory. `SCITEX_AGENT_CONTAINER_HOME`
selects a different SAC root. An existing invalid manifest or stale projection
refuses launch; the bundled defaults apply only when no host manifest exists.
Run `sac worktree policy generate-projections` after an intentional manifest change and
`sac worktree policy check-projections` to verify it.

## Launch behavior

Before a write-capable `kind: Agent` task starts, SAC:

1. asks the CLI to validate its generated projections;
2. inspects the authored `spec.workdir` through the CLI;
3. resolves a stable, agent-owned linked worktree and asks the CLI to authorize
   any provisioning command;
4. changes the in-memory runtime workdir to that linked worktree; and
5. asks the CLI to authorize edit context there immediately before launch.

`spec.workdir` is the authored project path. When it names the primary checkout,
SAC chooses the topic branch automatically from the agent name; there is no
branch-name field in the spec. The slug lowercases the name, replaces groups of
non-alphanumeric characters with `-`, strips edge hyphens, and keeps at most 48
characters. SAC appends the first eight hexadecimal characters of SHA-256 of
the original name:

- Folder: `<spec.workdir>/.worktrees/sac-<slug>-<name-hash>`.
- Branch: `feature/sac-<slug>-<name-hash>`.

For `scitex-infrastructure-lead`, these become
`.worktrees/sac-scitex-infrastructure-lead-68bd473b` and
`feature/sac-scitex-infrastructure-lead-68bd473b`.

To choose an existing topic branch explicitly, set `spec.workdir` to its linked
worktree path. The checker must approve that branch, and SAC records ownership
of a clean explicitly selected worktree. A dirty unowned linked worktree is
refused. An existing automatically selected target without SAC ownership is
also refused, even if clean; SAC does not silently adopt it.

Ownership is recorded at
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
reused worktree. A restart is checked before the old process is stopped;
a supervisor restart repeats the gate.

Any missing executable, timeout, denial, stale generated projection, malformed
response, invalid hash, identity change between checks, dirty/conflicting
ownership, or failed provisioning refuses launch. There is no permissive
fallback.

## Recovering an absent owned checkout

`sac agents restore-worktree NAME --expected-tip FULL_COMMIT_ID` is an explicit
dry-run operation on the agent's declared owning host. It reads the existing
canonical runtime ownership record and asks the neutral CLI's
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

The SAC suite uses a subprocess protocol fixture for hermetic mechanics tests.
To run its additional cross-repository contract against an explicitly configured policy CLI,
set `SCITEX_WORKTREE_POLICY_TEST_CLI` to its reviewed executable and run
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
workdirs and the CLI-approved context.

`kind: AgentProxy` is outside this gate because it launches no mutation-capable
agent harness.

## Capability boundaries and drift

| Surface | Responsibility |
| --- | --- |
| Packaged CLI + selected manifest | Sole executable decision contract and policy identity. |
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
