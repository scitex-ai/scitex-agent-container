# How sac works

`scitex-agent-container` (`sac`) materializes a `spec.yaml` into a long-lived,
externally addressable Claude agent.

## Launch flow

```
  spec.yaml   ─┐
  dot_claude/ ─┴─→ sac agents start ──→ apptainer instance
                                          │
                                          ▼
                              long-lived Claude SDK session
                              │
                              ├── <workdir>  (= spec.workdir, mounted rw)
                              │     CLAUDE.md / .mcp.json / .env / state.md     ← from dot_claude/
                              │     .claude/{commands,skills,hooks,...}         ← mirrored
                              │
                              ├── spec.mounts[]  ← explicit host-path allowlist (ro/rw)
                              │
                              ├── state-dir  (host: ~/.scitex/agent-container/runtime/<name>/)
                              │     pid, heartbeat.json,
                              │     session.jsonl, session_id, quota.json
                              │
                              ├─→ POST /v1/turn                 (per-agent A2A inbound)
                              │       ▲
                              │       │  live-runner route
                              │       │
  sac listen :7878 ───────────┼───────┘
  bearer-auth /v1/sac/{                 \
    health, agents,                      ─→ claude --resume <sid> -p
    agents/<n>/{status,send,card},                          (re-launch fallback when
    ...                                                      no live runner)
  }
                                                            ▲
  sac peer  post-turn  AGENT TEXT  ────────────────────────┘
```

## What each piece does

### `spec.yaml` (SSoT)

The single file that fully defines an agent. The agent name is the name of its
parent directory — no name field in the YAML. See [spec-reference.md](spec-reference.md).

### `dot_claude/` (optional)

A directory next to `spec.yaml`. At start, `sac` copies its contents into the
agent's `<workdir>`:

| Source                    | Destination                     | Merge rule             |
|---------------------------|---------------------------------|------------------------|
| `CLAUDE.md`               | `<workdir>/CLAUDE.md`           | marker-protected append |
| `.mcp.json`               | `<workdir>/.mcp.json`           | per-server merge       |
| `.env`                    | `<workdir>/.env`                | mode 0600, overwrite   |
| `state.md`                | `<workdir>/state.md`            | full overwrite         |
| `commands/`, `skills/`, `hooks/` | `<workdir>/.claude/*/`   | recursive copy         |

### Apptainer instance

`sac agents start` calls `apptainer exec` with:
- the SIF at `spec.apptainer.image`
- `<workdir>` bound rw at `/work`
- any extra binds from `spec.apptainer.binds[]`
- env vars from `spec.apptainer.env`
- optional GPU passthrough (`--nv` / `--rocm`)

### Claude SDK session

Inside the container, `sac` launches `claude` (Claude Code CLI) as a long-lived
SDK session. Session state persists in the host-side state dir so it survives
container restarts. `spec.claude.session` controls whether to start fresh
(`new-session`), continue the last session (`continue`), or resume a specific
one (`resume <sid>`).

### A2A inbound (`spec.a2a.port`)

When `spec.a2a.port` is set, `sac` binds `POST /v1/turn` on that localhost port
for this agent. Any process on the host can send a prompt without knowing the
tmux pane — including other agents via `sac peer post-turn`.

### `sac listen` (control plane)

A per-host REST API (bearer-auth, loopback-only) that exposes fleet-wide
operations: health checks, agent status, send, agent-card, and more.
`sac peer post-turn` routes through it for local agent-to-agent messaging.

## Restart / health

The runner supervisor checks `spec.health` probes and applies `spec.restart`
policy (never / on-failure / always) with exponential backoff.
Heartbeat state is written to `runtime/<name>/heartbeat.json` every tick.

## See also

- [spec-reference.md](spec-reference.md) — full field reference
- [directories.md](directories.md) — directory tree + config cascade
- [images.md](images.md) — Apptainer image management
