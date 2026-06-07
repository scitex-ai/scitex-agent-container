---
description: |
  [TOPIC] Where agent git-worktrees must (and must NOT) live to avoid silent reaping by the Claude Code harness.
  [DETAILS] The Claude Code SDK runtime auto-manages files under any `.claude*`-prefixed path (file-history GC + session-dir cleanup). Worktrees created at `<workdir>/.claude/worktrees/`, `<workdir>/.claude-worktrees/`, or any other `.claude*` path are visible to that machinery and can be reaped on an idle-time heuristic — losing uncommitted/unpushed work. Use a NEUTRAL path instead (`<workdir>/worktrees/`, `/tmp/<agent>-worktrees/`).
tags: [scitex-agent-container-worktree-path-safety]
---

# Worktree path safety — don't put worktrees under `.claude*/`

> Verified anchor: proj-paper-scitex-clew capsule-0220918 cohort-A
> incident, 2026-06-06. A worktree at
> `/work/.claude-worktrees/clear-stale-submission/` with uncommitted
> changes + a running pytest was reaped by the Claude Code harness on
> an "agent main-loop idle = stale" heuristic. Clew recovered via a
> last-minute `cp → /tmp/`; the next agent in the same shape won't be
> so lucky.

## The rule

**Never create a git worktree at any path whose name starts with
`.claude`.** That includes the obvious slash variant
`<workdir>/.claude/worktrees/` AND the hyphen sibling
`<workdir>/.claude-worktrees/`, AND any `.claude-*` parked-style dir.

## Why

The Claude Code SDK runtime owns the `.claude*` namespace inside an
agent's workdir. It walks those paths for file-history GC, session-dir
cleanup, and project-state snapshots; the operator's host-side
investigation (2026-06-06) traced clew's reaped worktree to harness
internals (`~/.claude/file-history/`, `~/.claude/projects/...-claude-
worktrees-agent-...`). The harness is not patchable from this
package's surface — sidestep it.

## Use a neutral path

The sac convention is **`<workdir>/worktrees/<branch-slug>/`** (no
`.claude` prefix). This is what `sac` and the in-container runner both
expect; the harness leaves it alone.

```bash
# RIGHT — outside the .claude namespace
git -C /work worktree add -b fix/my-thing /work/worktrees/fix-my-thing origin/develop

# WRONG — inside .claude/, the harness can reap this
git -C /work worktree add -b fix/my-thing /work/.claude/worktrees/agent-fix-my-thing origin/develop

# WRONG — inside .claude-*, the harness can reap this too
git -C /work worktree add -b fix/my-thing /work/.claude-worktrees/fix-my-thing origin/develop
```

For sandbox / scratch work that doesn't need to be next to the repo,
`/tmp/<agent>-worktrees/<branch>` is also safe — `/tmp` is outside any
`.claude` namespace.

## Defence-in-depth (already in place for the slash variant)

The in-package pruner `sac agents prune-claude` (the only thing in
THIS repo that reaps `.claude/worktrees/`) carries lead's five safety
predicates as of 2026-06-06: a worktree is preserved if (1) it has
uncommitted changes, (2) it has unpushed commits, (3) a live process
holds an fd into the dir, or (4) its branch is not merged into
`origin/develop`/`origin/main`. Idle-time is intentionally NOT used as
a primary signal; the work-loss predicates fire first. See
`cli_pkg/_agent_prune_claude.py`.

That defence covers the slash variant `.claude/worktrees/agent-*` that
sac itself manages. It does NOT cover the harness's reaping of
arbitrary `.claude*` paths — only the path-placement rule above does.
Both layers are load-bearing.

## What to do if you find a worktree already under `.claude*/`

1. **Stop and commit + push first** (`git -C <wt> status`,
   `git add -A && git commit -m "wip rescue"`, `git push -u origin <branch>`).
   The harness can reap the dir at any moment; salvage state before
   moving anything.
2. **Re-anchor under a neutral path:**

   ```bash
   git -C /work worktree add -b <branch> /work/worktrees/<slug> <branch>
   git -C /work worktree remove --force /work/.claude*/...   # only after #1
   ```

3. **Verify** with `git -C /work worktree list` that the new path
   sticks and the `.claude*` entry is gone.

## See also

- [40_troubleshooting.md](40_troubleshooting.md) — adjacent agent-launch
  troubleshooting; the worktree-path rule is structural and lives here
  rather than there.
- `cli_pkg/_agent_prune_claude.py` — slash-variant pruner with lead's
  five safety predicates; tests in
  `tests/scitex_agent_container/cli_pkg/test__agent_prune_claude.py`.
