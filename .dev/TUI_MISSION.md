# TUI runtime hedge — steps 2/3/4 mission

- Lead a2a: `d44adb9d` (step-1 spec) + `d383f5389dc548a49a293bffe390d619`
  (step-2/3/4 inline AC, this doc)
- Cutoff: **2026-06-15** (hard)
- Worktree: **this one** (`wt-tui-followup`, branch
  `feat/tui-runtime-e2e-integration`). Steps 2-4 EXTEND step 1
  (commit `509a6de1`) and ship as ONE integration PR.
- Owner: proj-scitex-agent-container
- Created: 2026-06-14 (restart-resilience: lead asked this be written
  to disk so the next session restart finds the mission here rather
  than losing it with the conversation buffer).

---

## Why this file exists

A prior session restart (2026-06-14) wiped working memory mid-mission.
Lead re-issued the plan; this file is the durable copy so any future
restart can re-orient by reading `/work/.worktrees/wt-tui-followup/.dev/TUI_MISSION.md`
and resume.

If you are a restarted agent reading this: check `git log` of this
worktree against the section "Status checkpoints" at the bottom to
find where the previous session got to, then continue from the next
unchecked step.

---

## Step 1 — DONE (commit 509a6de1)

`TuiSessionRuntime` materialises `to_home/` + `CLAUDE.md` into
`<state>/home/` and exports `HOME` + `CLAUDE_CONFIG_DIR` into the
tmux session before launching `claude`. New public helpers:
`state_dir_for_config(config)` and `materialize_workspace(config)`.

---

## Step 2 — real tmux + real claude smoke (no fake mux)

Spawn a `spec.runtime: tui` test agent, start it, confirm:

- (a) tmux session spawns
- (b) `claude` TUI launches **inside** tmux with `HOME=<state>/home`
- (c) the materialised `to_home/` is present in `<state>/home/`:
  `CLAUDE.md`, `.mcp.json`, `skills/`, `settings.json`

Verification commands (record outputs in the step-2 commit body):
- `tmux list-sessions` shows the session.
- `tmux capture-pane -p -t <session>` shows the claude TUI banner.
- `ps -ef | grep -E 'tmux|claude'` shows `claude` parented under
  `tmux`.
- `tr '\0' '\n' </proc/<claude-pid>/environ | grep -E '^HOME='`
  shows the materialised path.

AC: claude TUI is running inside tmux with the materialised HOME.

## Step 3 — THE acceptance gate (one verified turn)

Drive ONE a2a turn through the TUI runtime end-to-end:

- Send the test agent a message via the runtime's normal input path.
- Confirm claude TUI ingests the input and produces an output turn.
- Confirm the turn is observable through the runtime (capture-pane
  diff or sidecar log), not just by eyeballing tmux.

AC: a real agent completes one verified turn via the TUI runtime.

## Step 4 — tui-alive probe

`TuiSessionRuntime.is_running` must mean **"claude is responsive"**,
not "tmux session exists". Two acceptable signals:

- pane-activity timestamp (`tmux display -p '#{session_activity}'`
  advanced within the last N seconds), OR
- sidecar heartbeat (claude prints a heartbeat token periodically
  and the runtime scrapes the pane).

Pick the cheaper one (likely pane-activity).

---

## Test-agent spec

No canonical `runtime: tui` spec stub exists yet — author one as part
of this work. Minimal shape (extend an existing claude-session spec
and flip `runtime: tui`):

- `runtime: tui`
- a trivial mission (echo back / "say hi to lead")
- an a2a port (required for step-3 turn delivery)
- minimal skills (enough for the agent to know how to a2a-reply)

Save under `tests/scitex_agent_container/runtimes/_fixtures/` (or the
existing fixture dir for runtime tests — match the pattern).

---

## Worktree + PR contract

- Branch: `feat/tui-runtime-e2e-integration` (already exists).
- One PR for steps 1-4 once step 3 passes. Step 4 may land in the
  same PR or as a fast-follow if step 3 reveals it needs more design.
- TDD discipline: every new module gets tests; no mocks of real
  tmux / real claude (the whole point of step 2 is killing the fake).
- Full suite green before push.
- No `Co-Authored-By: Claude` trailer. No `--force` / `--no-verify`.

---

## Status checkpoints

Update this section as steps complete. Pattern: `[x]` + commit SHA +
short note.

- [x] step 1 — `509a6de1` materialise to_home + CLAUDE.md
- [x] step 2 — `3f65122f` real tmux+claude smoke (test_tui_session_real_smoke.py)
- [x] step 3 — `2e4f1a0b` send_turn + nonce round-trip (hermetic per lead a2a edfe809e)
- [ ] step 4 — tui-alive probe (pane-activity or heartbeat)
- [ ] integration PR opened (link here)
