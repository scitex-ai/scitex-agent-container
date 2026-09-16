# Harness-neutral worktree policy gate

SAC does not own worktree policy. Before it starts a write-capable agent task,
it invokes the operator-owned executable at
`~/.dotfiles/src/.bin/scitex-worktree-policy`. That CLI and its adjacent
manifest are the runtime authority; SAC contains no branch, worktree-location,
exception, or bypass rules.

## Observed launch behavior

For every production `kind: Agent` launch, including Claude Code, Codex, and
Hermes harnesses, SAC performs these host-side calls before replacing or
starting the harness process:

```text
scitex-worktree-policy check-projections
scitex-worktree-policy assert-context --repo <resolved spec.workdir> --intent edit
```

The launch proceeds only when both processes exit zero, both JSON objects are
well formed, projections report `current`, context reports an explicit
`allow`, and both responses carry the same valid `policy_sha256` and
`projection_sha256`. A missing executable, timeout, denial, stale projection,
malformed response, absent hash, or changing hash refuses the launch. There is
no permissive fallback. A forced restart is checked before the old process is
stopped. A supervisor restart repeats the calls; a prior result is provenance,
not reusable permission.

The gate runs on the host deliberately. Agent containers do not need a copy of
the dotfiles tree or a harness-specific hook. The resolved proof is stored on
the incarnation row as `policy_sha256` and `projection_sha256`, and under
`compiled_spec_json.launch_artifacts.worktree_policy` with the context returned
by the CLI.

`kind: AgentProxy` is outside this gate because it launches no agent harness
with shell/file mutation tools. `--dry-run` and an already-running no-op do not
grant a new write-capable task and therefore do not claim a new policy proof.

## Capability boundaries

| Surface | Responsibility |
| --- | --- |
| CLI | Sole deterministic decision engine and JSON/exit-code contract. |
| SAC lifecycle | Host-side adapter: invoke, validate, refuse, and record proof. |
| MCP | Transport only; any future MCP surface must invoke the CLI rather than reproduce decisions. |
| Hooks | Event adapters only. Claude hooks are not the cross-harness authority. |
| Skills/prompts | Discovery and guidance only; they cannot authorize a denied launch. |
| Commands | Optional user-experience wrappers around the CLI, never policy. |

## Drift discipline

This document describes SAC's tested adapter behavior, not the policy's current
rules. Inspect the CLI/manifest for those rules. Generated AGENTS, CLAUDE, and
HERMES projections are checked by the CLI at launch and identified by hash;
editing prose or prompts cannot change the launch decision.

The subprocess contract is covered with an executable fixture at
`tests/scitex_agent_container/_lifecycle/_fixtures/worktree-policy/`. The
fixture models responses, not policy rules. This keeps tests capable of proving
the real process boundary without installing a second runtime policy engine.
