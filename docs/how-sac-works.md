# How sac works

`scitex-agent-container` (`sac`) materializes a `spec.yaml` into a long-lived,
externally addressable Claude agent.

## Launch flow

```
  spec.yaml   ─┐
  to_home/    ─┴─→ sac agents start ──→ apptainer instance
                                          │
                                          ▼
                              long-lived Claude SDK session
                              │
                              ├── $HOME  (= runtime/<name>/home/, bind-mounted)
                              │     CLAUDE.md / .mcp.json / .env / state.md     ← from to_home/
                              │     .claude/{commands,skills,hooks,...}         ← from to_home/.claude/
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

### `to_home/` (optional)

A directory next to `spec.yaml`. At start, `sac` mirrors its contents into the
agent's container `$HOME` (= `runtime/<name>/home/`). Every path under
`to_home/` lands at the same relative path under `$HOME`:

| Source                    | Destination                     | Merge rule             |
|---------------------------|---------------------------------|------------------------|
| `CLAUDE.md`               | `$HOME/CLAUDE.md`               | marker-protected append |
| `.mcp.json`               | `$HOME/.mcp.json`               | full overwrite         |
| `.env`                    | `$HOME/.env`                    | mode 0600, overwrite   |
| `state.md`                | `$HOME/state.md`                | marker-protected append |
| `.claude/{commands,skills,hooks}/` | `$HOME/.claude/*/`     | recursive copy         |

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
