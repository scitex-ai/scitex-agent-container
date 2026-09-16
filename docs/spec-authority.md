# Lifecycle spec authority

`sac agents start` and every lifecycle path that reaches `agent_start` fail
closed before runtime construction. A diagnostic warning is not launch
permission: if SAC cannot prove where a spec came from, it refuses the launch.

## Accepted sources

Exactly two source shapes are accepted:

1. **Live authority:** the repository's main checkout is on `develop`, its
   working tree is completely clean, `develop` tracks a `develop` upstream,
   a fresh `git fetch --prune` succeeds, and HEAD is neither ahead nor behind.
2. **Immutable authority snapshot:** a clean detached checkout at
   `.../sac-authority/<source>-<40-hex-commit>/`. The suffix must equal HEAD,
   `<source>` must equal the normalized `origin` repository identity, and the
   loaded spec's blob digest must equal the blob recorded at HEAD.

The snapshot rule is intentionally exact. It preserves reproducible detached
authorities without treating an arbitrary detached checkout as trustworthy.

## Refused sources

Lifecycle preflight refuses all of the following:

- a non-git or unresolvable source;
- a missing, malformed, or unreachable upstream;
- any dirty file in the authority repository, including untracked files;
- a main checkout on a branch other than `develop`;
- a linked feature worktree used as live authority;
- a live checkout that is ahead, behind, or diverged;
- a detached checkout outside the immutable snapshot convention;
- a snapshot whose source name, HEAD, or spec blob does not match its identity.

There is no `--allow-stale-spec`, environment-variable escape hatch, generic
`--force`, or exception fallback around this gate. `--strict-drift` remains a
compatibility spelling for callers that already pass it, but cannot change the
policy. Feature development belongs in linked worktrees; linking a feature
spec into the live registry does not promote it to authority.

This enforcement belongs to the core Python lifecycle boundary. The CLI and
MCP-facing launch adapters converge on `agent_start`; neither owns a separate
policy. Hooks may report or prevent edits, but are not lifecycle authority.
Skills and prompts explain the contract to agents, but cannot grant permission
or compensate for a failed check. Configuration names the desired agent; it
does not override the provenance of the configuration itself.

## Behavior is the source of truth

The executable policy is
`scitex_agent_container._drift._authority.validate_spec_authority`. Its
real-git contract tests are in
`tests/scitex_agent_container/_drift/test__authority.py`; lifecycle wiring is
covered by `tests/scitex_agent_container/_lifecycle/test__start_drift.py`.
Update those tests with any policy change before updating this document or an
agent-facing skill. `sac doctor` remains a read-only diagnostic surface and
may report unknown states; lifecycle admission never inherits that leniency.
