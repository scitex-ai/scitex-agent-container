---
description: |
  [TOPIC] SAC's harness-neutral, fail-closed worktree policy launch gate.
  [DETAILS] The external scitex-worktree-policy CLI is sole authority; SAC validates and records its proof before Claude, Codex, or Hermes tasks start.
tags: [scitex-agent-container-worktree-policy, cli, hooks, mcp, skills]
---

# Worktree policy gate

Use [`docs/worktree-policy-gate.md`](../../../../docs/worktree-policy-gate.md)
as the behavior reference.

The operational distinction is load-bearing:

- `scitex-worktree-policy` is the only decision engine.
- SAC is a launch adapter and proof recorder.
- MCP and commands are transports/wrappers, not alternate policy engines.
- hooks adapt harness events but cannot be the common Claude/Codex/Hermes gate.
- skills, generated fragments, and prompts aid discovery only.

Before diagnosing a refusal, run the exact CLI calls SAC reports against the
resolved workdir. Do not infer an exception from a skill, hook, prompt, or old
document. A nonzero exit, invalid JSON, stale projection, or missing/mismatched
hash is a refusal by construction.
