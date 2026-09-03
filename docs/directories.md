# Configuration and Runtime Directories

## Layout

Configuration is separated into user-scope and project-scope. Project-scope (`.scitex/agent-container/` at the git root) takes priority when present.

```
~/.scitex/agent-container/ or <project>/.scitex/agent-container/
├── config.yaml                ← host identity, host.aliases, peers (F-CS12),
│                                listen.{host,port}, a2a.port_range,
│                                scratch_root (+ scratch_root_reason)
├── agents/<name>/             ← per-agent declarations (you write these)
│   ├── spec.yaml              ← v3 Agent definition (the SSoT)
│   └── to_home/               ← optional: mirrored into the agent $HOME at start.
│                                Any path lands at the same relative path; the
│                                entries below are the Claude Code harness's
│                                filenames, shown as the worked example.
│       ├── CLAUDE.md           (→ $HOME/CLAUDE.md, marker-protected)
│       ├── .mcp.json           (→ $HOME/.mcp.json, full overwrite)
│       ├── .env                (→ $HOME/.env, mode 0600)
│       ├── state.md            (→ $HOME/state.md, marker-protected)
│       └── .claude/
│           ├── commands/       (→ $HOME/.claude/commands/)
│           ├── skills/         (→ $HOME/.claude/skills/)
│           └── hooks/          (→ $HOME/.claude/hooks/)
├── accounts/                  ← provider account store
│   ├── <name>/
│   │   ├── account.json        (safe metadata; no tokens)
│   │   └── .credentials.json   (copied into ~/.claude/ on `sac accounts switch`)
│   └── openai/<name>/
│       ├── account.json        (safe metadata; no tokens)
│       └── auth.json           (mode 0600; collected by `sac accounts sync-openai`)
│   └── _rotations/
│       └── <email>.ndjson      (OAuth-rotation log, one append per observed rotation)
├── tokens/
│   └── listen-<host>.token    ← `sac listen` bearer tokens (0600)
├── containers/                ← built Apptainer images
│   ├── sac-base.sif    -> sac-base/sac-base.sif        (top-level symlink)
│   ├── sac-scitex.sif  -> sac-scitex/sac-scitex.sif    (top-level symlink)
│   ├── sac-{base,scitex}/                              (dir-per-image)
│   │   ├── sac-{base,scitex}.sif                       (the image; gitignored)
│   │   ├── sac-{base,scitex}.def                       (recipe snapshot)
│   │   ├── sac-{base,scitex}.build-YYYY-MMDD-HHMMSS.log (full build log; gitignored)
│   │   └── .def-hash                                   (skip-rebuild cache)
│   └── {dpkg,node,requirements}-lock.txt               (auto-freeze lock files)
└── runtime/                   ← regenerable per-host state; gitignored
    ├── <agent-name>/           per-agent runner state
    │   ├── pid                  (runner PID)
    │   ├── heartbeat.json       ({ts, pid, state}; refreshed every tick)
    │   ├── session_id           (persisted SDK session id, resume marker)
    │   ├── session.jsonl        (one JSON object per turn event)
    │   └── quota.json           (accumulated per-turn token totals)
    ├── events/                  Claude Code hook event ring-buffer
    │   └── <agent>.jsonl
    └── cache/                   snapshot cache for the dashboard / `sac agents diff`
        └── <agent>.{latest,prev,diff}.json
```

Not under this root, and deliberately so — the SCRATCH volume (ADR-0024):

```
<scratch_root>/                ← `scratch_root:` in config.yaml, else /scratch
└── sac/agents/<name>/uvwork   ← bound at /uvwork inside the container (0700).
                                 uv itself, the uv cache, TMPDIR and the agent
                                 venv (/uvwork/venv-agent). It lives HERE rather
                                 than in the overlay upper because the overlay
                                 is on the host's ROOT LV, which filled to 0
                                 four times on scitex-compute-04 on 2026-09-02
                                 (11.7 GB of it was one agent's /uvwork).
                                 Regenerable: the spec rebuilds it on the next
                                 start if it is missing. Move an existing
                                 overlay copy across with
                                 `sac agents scratch-migrate`.
```

## Configuration cascade

Settings are resolved in this order (first wins):

1. **CLI flag** — `sac agents start hello --workdir /tmp/x`
2. **Env var** — `SAC_<X>` or the long `SCITEX_AGENT_CONTAINER_<X>` form
   (setting both with different values raises `SacEnvConflict`). Copy
   [`.env.example`](../.env.example) to `.env` and uncomment what you need.
3. **Project config** — `<proj>/.scitex/agent-container/config.yaml`,
   when you're inside a git repo that ships one.
4. **User config** — `~/.scitex/agent-container/config.yaml`
   (relocatable via `$SCITEX_DIR`).
