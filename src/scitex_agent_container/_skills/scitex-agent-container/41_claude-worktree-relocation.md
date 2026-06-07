---
description: |
  [TOPIC] Claude Code worktree relocation hooks (F-CS8 prevention)
  [DETAILS] Canonical WorktreeCreate/WorktreeRemove hook scripts that relocate
  Claude Code's default `.claude/worktrees/<name>` creations to
  `<git-root>/.worktrees/<name>` where the operator's prune cron maintains
  hygiene. Stops the recurring multi-GB / 100k-file accumulation that wedges
  agents (F-CS8 recurrence ~100×, neurovista wedge 22.9 GB / 80k files).
tags: [scitex-agent-container-claude-worktree]
---

# Claude Code worktree relocation — F-CS8 prevention

Claude Code's bundled binary creates session/agent worktrees under
`.claude/worktrees/<name>` by default. That tree is never self-cleaned
and accumulates multi-GB / 100k-file bloat that wedges agents (F-CS8
recurrence ~100×; wedged neurovista at 22.9 GB / 80k files → 0-output
turns). The operator's daily prune cron maintains hygiene on
`<root>/.worktrees/` (mtime-gated, never touches `.claude/`). The
`WorktreeCreate` / `WorktreeRemove` hooks land the prevention side of
the same policy: relocate every creation to the cron-maintained tree.

## Canonical asset location

The verified hook scripts + settings fragment live in this repo at:

    src/scitex_agent_container/_baseline_assets/claude_worktree_hooks/
      ├── worktree_create.py
      ├── worktree_remove.py
      ├── settings.local.json.fragment.json
      └── README.md

Regression tests:

    tests/scitex_agent_container/_baseline_assets/claude_worktree_hooks/
      └── test_worktree_hooks.py

The tests drive the real scripts as subprocesses against an ephemeral
on-disk git repo (`tmp_path` + `git init`) — no mocks, no patches. They
pin the I/O contract, the relocation invariant, the `claude/<name>`
branch policy, the idempotence guarantees, and the loud-fail
diagnostics.

## Deployment (operator-side)

1. Copy both `worktree_create.py` and `worktree_remove.py` into the
   baseline to_home tree:

       <agents_dir>/_base/to_home/.claude/hooks/claude_worktree_hooks/

2. Merge the `hooks` block from
   `settings.local.json.fragment.json` into
   `<agents_dir>/_base/to_home/.claude/settings.local.json` (under the
   top-level `hooks` key). Preserve the `_comment_worktree_hooks`
   string — it carries the source-of-truth audit trail.

3. Restart agents (or wait for next natural restart). NO SIF REBUILD.
   The runtime's `deploy_to_home` (see `runtimes/_to_home.py`)
   materializes the baseline tree into every agent's `$HOME` on every
   start, so the hooks + settings land before Claude Code reads its
   config.

## Verification before fleet-wide

The hook event names + the I/O shape were verified 2026-06-06 against
the deobfuscated `executeWorktreeCreateHook` (`VRH`) function in the
`claude_agent_sdk._bundled.claude` binary shipped with the runner's
venv. The regression tests pin both. The live-probe on
proj-scitex-agent-container's runtime container (2026-06-06) drove the
real hook against the real `/work` repo and:

* `worktree_create.py` echoed `/work/.worktrees/live-probe-1`, created
  the dir, registered it in `git worktree list`, and put it on branch
  `claude/live-probe-1` based at `origin/develop`.
* `worktree_remove.py` unregistered the worktree on the matching
  payload, exited 0.

Before broad deploy on any new SDK version, re-run the live-probe and
the regression tests — the SDK is the contract source-of-truth and
silent schema drift would break the relocation.

## Why this lives in sac, not in dotfiles

The hook scripts are canonical, version-controlled, regression-tested
implementations of a fleet-wide policy. Shipping them here (instead of
ad-hoc in the operator's dotfiles `_base/to_home/`) means:

* The implementation has an audit trail (commit history + PR review).
* The hook contract is pinned by tests, not by the operator's memory.
* SDK upgrades have a single place to re-verify.
* The dotfiles baseline syncs from a single source-of-truth path.

The deployment step (copy + settings-merge) is operator-driven because
the dotfiles tree is the operator's, not sac's. Future work could
package this as a `sac to_home seed-baseline` CLI command that pulls
the canonical assets into the dotfiles baseline tree automatically.
