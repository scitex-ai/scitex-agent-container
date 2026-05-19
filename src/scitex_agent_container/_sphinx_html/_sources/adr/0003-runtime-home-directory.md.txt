---
orphan: true
---

# ADR-0003: Runtime `home/` directory as the canonical container HOME (2026-05-14)

**Status:** Accepted.
**Supersedes:** Implicit layout from `runtime/agents/<name>/` + `runtime/workspaces/<name>/` (both unused after this ADR).
**Related:** [`0001-isolation-hardening.md`](0001-isolation-hardening.md) — D5 introduced `--home /home/agent` but didn't relocate the host-side directory the container HOME points at.

## Problem

After D5 (`docs/adr/0001-isolation-hardening.md`), sac auto-injects
`--home /home/agent` so `$HOME` is operator-independent inside the
container. But `_dot_claude.py::deploy_dot_claude(config, workdir)`
still materialises `dot_claude/*` into `<host-workdir>/.claude/`,
and the host workdir is bind-mounted at **`/work`** — not at
`/home/agent`.

Consequence:

- `/work/.claude/CLAUDE.md` is found by Claude's auto-walk-from-cwd
  (still works for CLAUDE.md).
- `/home/agent/.claude/skills/`, `/home/agent/.claude/.mcp.json`,
  `/home/agent/.claude/hooks/`, `/home/agent/.claude/commands/` — empty.
  Claude SDK's `$HOME/.claude/`-rooted discovery never sees the
  operator's `dot_claude/` contents.

The runtime layout also carries vestigial empty directories
(`runtime/agents/<name>/`, `runtime/workspaces/<name>/`) from earlier
designs that were never wired through, which makes the "where does
sac actually put things?" question ambiguous.

## Decision

### D6. Per-agent runtime directory = the container's `$HOME` source.

For each agent, sac maintains:

```
~/.scitex/agent-container/runtime/<name>/
├── home/                  ← bind-mounted at /home/agent (canonical $HOME)
│   ├── .claude/           ← dot_claude materialised here
│   │   ├── CLAUDE.md
│   │   ├── .mcp.json
│   │   ├── skills/
│   │   ├── hooks/
│   │   └── commands/
│   └── (anything the agent writes to $HOME — proj/, .cache/, ...)
├── overlay.img            ← per-agent writable layer (was: runtime/overlays/<name>.img)
├── pid
├── apptainer_pid
├── heartbeat.json
├── session_id
├── session.jsonl
├── quota.json
└── stdout.log
```

The bind:

```
~/.scitex/agent-container/runtime/<name>/home  →  /home/agent  (rw)
```

So inside the container:

- `$HOME == /home/agent` (D5 invariant).
- `$HOME/.claude/skills/` resolves to the materialised tree —
  Claude SDK's discovery works without further config.
- Anything the agent writes under `$HOME` persists on the host at
  `runtime/<name>/home/`, browsable from the operator side without
  entering the container.

### D7. `dot_claude` materialises into `runtime/<name>/home/.claude/`.

`_dot_claude.py::deploy_dot_claude` is rewritten to:

1. Resolve `runtime/<name>/home/.claude/` as the target.
2. Materialise `dot_claude/CLAUDE.md → home/CLAUDE.md`,
   `dot_claude/.mcp.json → home/.mcp.json`, `dot_claude/.env →
   home/.env`, `dot_claude/state.md → home/state.md`.
3. Mirror `dot_claude/{commands,skills,hooks,agents,settings,...}/`
   → `home/.claude/<same>/`.

The workdir `/work` (which still mounts the operator's host workdir)
no longer receives a `.claude/` materialisation — it stays a pure
project-content mount.

### D8. Overlay path consolidation.

`apptainer.overlay` default resolves to `runtime/<name>/overlay.img`
(previously `runtime/overlays/<name>.img`). Per-agent state lives
under `runtime/<name>/` exclusively; the old `runtime/overlays/`
location is left in place for backward compatibility but new agents
write to the new location.

### D9. Vestigial directories deprecated.

`runtime/agents/<name>/` and `runtime/workspaces/<name>/` are no
longer used by sac. They remain in the filesystem for now (no
destructive cleanup); a future `sac runtime cleanup` command can
remove them.

## Migration

- **Existing agents** — on next `sac agents start`:
  - `runtime/<name>/home/` is auto-created if missing.
  - `dot_claude` re-materialises into the new location.
  - Existing `runtime/<name>/{pid,session.jsonl,…}` files keep their
    current location (this ADR doesn't move them).
  - Existing `runtime/overlays/<name>.img` continues to work if
    `apptainer.overlay` points at it explicitly; new agents get
    `runtime/<name>/overlay.img` by default.
- **No data loss** — the refactor adds a directory; doesn't delete
  or move existing data.

## Rationale

### Why name the directory `home/`, not `workspace/`?

The semantic name (`home/`) directly matches what it becomes inside
the container (`/home/agent`). Operators reading
`runtime/scitex-stats-auditor/home/.claude/skills/` instantly know
"this is what the agent sees as `~/.claude/skills/` inside the
container." `workspace/` requires a glossary lookup.

The bind line is self-documenting:

```
runtime/<name>/home  →  /home/agent
       (host home)      (container home)
```

This matches Kubernetes' philosophy of naming things by their
container-side semantic (`volumeMounts.mountPath`), not the host-side
implementation detail.

### Why `home/` *inside* `runtime/<name>/`, not as a sibling?

Per-agent state should be browsable as one directory tree. Operators
running `du -sh runtime/<name>/` should see the total footprint of
that agent (overlay + home + logs + session). Splitting across
parallel trees (`runtime/<name>/`, `runtime/overlays/<name>.img`,
`runtime/workspaces/<name>/`) makes the "what does this agent use?"
question harder than it needs to be.

### Why not symlink `/home/agent → /work`?

- Symlinks inside containers interact poorly with apptainer's bind
  scaffolding (the same class of bugs as the original D4 motivation).
- Two distinct mounts (one at `/work`, one at `/home/agent`) makes
  the semantic separation visible: `/work` = project source code
  (often read-only), `/home/agent` = agent's own state + skills.

### Relationship to "strict vs relaxed" deployment modes

This ADR is layout-only. The strict-vs-relaxed mode distinction
(Clew reproducibility runs vs everyday development) layers on top:

- **strict**: `runtime/<name>/home` bound `:ro`, project sources
  `:ro`, no shared skills bind. Promises the verification chain
  that `$HOME` was immutable during the run.
- **relaxed**: `runtime/<name>/home` bound `:rw`, project sources
  potentially `:rw`, host-side `~/.claude/skills/` optionally bound
  in for cross-agent skill sharing.

A separate ADR (0004) will formalise the mode field. This ADR just
gives both modes a sensible directory to bind.

## Implementation

| Layer | Where | Status |
|---|---|---|
| Auto-create `runtime/<name>/home/` on start | `_apptainer_runtime.py::start` | ⏳ this PR |
| Auto-prepend bind `home:/home/agent:rw` | `_apptainer_runtime.py::build_run_argv` | ⏳ this PR |
| Materialise `dot_claude/` into `runtime/<name>/home/.claude/` | `_dot_claude.py::deploy_dot_claude` | ⏳ this PR |
| Default `apptainer.overlay` → `runtime/<name>/overlay.img` | `_apptainer_runtime.py` resolution | ⏳ this PR |
| ADR + `docs/isolation.md` cross-ref | docs | ✅ this commit |

## Consequences

**Positive.**
- Claude SDK's `$HOME/.claude/`-rooted discovery (skills, hooks,
  `.mcp.json`) works without operator intervention.
- Per-agent state lives in one tree (`runtime/<name>/`) — backup,
  cleanup, and inspection commands stay simple.
- Operator-side debugging: `ls runtime/<name>/home/` shows what the
  agent sees as its `$HOME` — no container shell needed.
- Layout name aligns with container-side semantic — fewer
  glossary jumps in docs.

**Negative / tradeoffs.**
- One extra bind in every container's argv. Negligible startup cost.
- `dot_claude/` materialised twice during the transition window:
  once into `runtime/<name>/home/.claude/` (new) and old
  `<workdir>/.claude/` may still exist on disk from prior runs.
  Cleanup deferred; not a correctness issue.

## References

- [`0001-isolation-hardening.md`](0001-isolation-hardening.md) — D5 introduced `--home /home/agent`.
- [`../isolation.md`](../isolation.md) — to be updated with the new layout description.
- [`../spec-reference.md`](../spec-reference.md) — `spec.dot_claude` materialisation target.
