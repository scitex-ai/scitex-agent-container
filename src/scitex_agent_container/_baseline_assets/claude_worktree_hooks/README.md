# Claude Code worktree-relocation hooks

Canonical, version-controlled implementation of Claude Code's
`WorktreeCreate` / `WorktreeRemove` hooks for the SAC fleet. Verified
2026-06-06 against the `executeWorktreeCreateHook` function in the
`claude_agent_sdk._bundled.claude` binary shipped with the runner's
venv.

## Why

Claude Code's default worktree mode creates `.claude/worktrees/<name>`
under the project root. That tree is never self-cleaned and
accumulates multi-GB / 100k-file bloat that wedges agents (F-CS8
recurrence ~100×; wedged neurovista at 22.9 GB / 80k files → 0-output
turns). The operator's daily prune cron already maintains hygiene on
`<root>/.worktrees/` — these hooks relocate creations there so
prevention and cleanup meet in the same tree.

## What

- `worktree_create.py` — reads hook input on stdin, creates
  `<git-root>/.worktrees/<name>` via `git worktree add` on a fresh
  `claude/<name>` branch off `origin/develop` (or HEAD as fallback),
  echoes the absolute path on stdout. Idempotent on re-trigger.
- `worktree_remove.py` — reads hook input on stdin, runs
  `git worktree remove` (with a `--force` second pass on failure).
  Idempotent.
- `settings.local.json.fragment.json` — the hooks-section snippet to
  merge into the baseline `to_home/.claude/settings.local.json`.

## How to deploy fleet-wide

1. Copy `worktree_create.py` and `worktree_remove.py` to the baseline:

       <agents_dir>/_shared/to_home/.claude/hooks/claude_worktree_hooks/

   The runtime's `_to_home.py` materializes that tree into every
   agent's `$HOME/.claude/hooks/claude_worktree_hooks/` on each start.

2. Merge the `hooks` block from `settings.local.json.fragment.json`
   into `<agents_dir>/_shared/to_home/.claude/settings.local.json`
   (under the top-level `hooks` key). The fragment's `_comment_*`
   string carries the source-of-truth audit trail and should be
   preserved.

3. Restart agents (or wait for next natural restart). NO SIF REBUILD
   IS NEEDED: `_to_home.py` runs on every agent start, the hook
   scripts and the settings file land in `$HOME` from the host-side
   baseline at materialize time, and Claude Code reads
   `~/.claude/settings.local.json` on each session boot.

## Hook contract — what the SDK guarantees and demands

`WorktreeCreate` input (stdin, JSON):

```json
{
  "hook_event_name": "WorktreeCreate",
  "name": "<worktree-name>",
  "cwd": "<dir-the-session-launched-from>",
  "session_id": "<uuid>",
  "transcript_path": "<path>",
  "permission_mode": "<mode>",
  "agent_id": "<id>",
  "agent_type": "<type>"
}
```

Required output: a single line on stdout = absolute path of the
created worktree (which MUST be a real directory by the time the
process exits). Any other shape → Claude Code rejects with
`WorktreeCreate hook failed: hook succeeded but returned no worktree
path` or `... returned a path that is not a directory`.

`WorktreeRemove` input mirrors the above with `worktree_path`
replacing `name`. The hook just needs to succeed; stdout is not read.

## Verification

Regression tests in
`tests/scitex_agent_container/_baseline_assets/claude_worktree_hooks/`
drive both hooks against a real ephemeral git repo (no mocks, no
patches) and assert: target path lands under `.worktrees/`,
`git worktree list` reflects it, idempotent re-trigger emits the same
path, invalid input fails loud, remove tears it down cleanly.

## Source-of-truth audit

The hook event names + I/O shape were verified against the
deobfuscated `_bundled/claude` binary's `VRH` function:

```js
async function VRH(H){
  let $ = {...b5(void 0), hook_event_name: "WorktreeCreate", name: H};
  let q = await O2({hookInput: $, timeoutMs: Y_});
  let K = q.find(A => A.succeeded && A.output.trim().length > 0);
  if (!K) {
    if (q.length === 0)
      throw Error("WorktreeCreate hook failed: hook is configured but did not run...");
    ...
  }
  return {worktreePath: K.output.trim()};
}
```

(and `x$$` for WorktreeRemove with `worktree_path`). If a future SDK
upgrade changes the schema, the regression tests below will detect it
on probe — DO NOT skip the live-probe step before fleet-wide rollout.
