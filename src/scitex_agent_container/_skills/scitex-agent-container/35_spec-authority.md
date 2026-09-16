---
description: |
  [TOPIC] Fail-closed lifecycle authority for agent specs.
  [DETAILS] Start accepts only a clean/current develop main checkout or an exact detached sac-authority source/commit/spec-digest snapshot. Unknown, unreachable, dirty, wrong-branch, linked-feature, ahead, behind and diverged sources refuse with no bypass.
tags: [scitex-agent-container-spec-authority, spec, authority, drift, worktree, snapshot]
---

# Lifecycle spec authority

Before runtime construction, every lifecycle start proves the spec source.
Only these shapes pass:

- the repository's clean main checkout on `develop`, exactly current with its
  freshly fetched `develop` upstream; or
- a clean detached `sac-authority/<source>-<40-hex-commit>` checkout whose
  normalized origin identity, HEAD, and loaded spec blob all match the path.

Everything unknown or mutable refuses: non-git, unreachable, dirty,
wrong-main-branch, linked feature worktree, ahead, behind, diverged, or a
misidentified detached snapshot. There is no launch override.

Feature branches belong in linked worktrees for development, but a linked
feature spec must never become live authority merely because a registry
symlink points at it. `--strict-drift` is compatibility syntax only and cannot
weaken enforcement.

The core Python lifecycle owns this gate. CLI and MCP launch surfaces converge
there; hooks can report or protect edits but do not authorize launches; this
skill is explanatory prompt material and cannot override executable policy.

Behavior authority: `_drift/_authority.py`. Real-git policy tests:
`tests/scitex_agent_container/_drift/test__authority.py`. Full operator-facing
contract: `docs/spec-authority.md`. If behavior changes, change the tests first
and then update this note; never infer current behavior from this prompt alone.
