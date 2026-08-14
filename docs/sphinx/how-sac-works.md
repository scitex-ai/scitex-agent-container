# How sac works

`scitex-agent-container` (`sac`) materializes a `spec.yaml` into a long-lived,
externally addressable agent process — whichever harness drives its turns.

## Launch flow

```
  spec.yaml   ─┐
  to_home/    ─┴─→ sac agents start ──→ apptainer instance
                                          │
                                          ▼
                              long-lived harness session (TUI or SDK)
                              │
                              ├── $HOME  (= runtime/<name>/home/, bind-mounted)
                              │     e.g. CLAUDE.md / .mcp.json / .env / state.md ← from to_home/
                              │     e.g. .claude/{commands,skills,hooks,...}     ← from to_home/.claude/
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
  sac channel send TO MSG ─────────────────────────────────┤
  sac peer  post-turn  AGENT TEXT  ────────────────────────┘
```

## What each piece does

### `spec.yaml` (SSoT)

The single file that fully defines an agent. The agent name is the name of its
parent directory — no name field in the YAML. See [spec-reference.md](spec-reference.md).

### `to_home/` (optional)

A directory next to `spec.yaml`. At start, `sac` mirrors its contents into the
agent's container `$HOME` (= `runtime/<name>/home/`). Every path under
`to_home/` lands at the same relative path under `$HOME` — the mirror itself is
harness-agnostic; the rows below are the Claude Code harness's filenames as the
worked example:

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

### Harness session

Inside the container, `sac` launches the *harness* — the agent program that
drives one turn. sac owns the process, its lifecycle, and its addressability;
the harness owns only the turn. Session state persists in the host-side state
dir so it survives container restarts.

The harness is selected by `spec.harness` (family) plus `spec.runtime` (launch
mode within that family). Four harnesses are registered; **only the `anthropic`
ones can be started today.** A registry entry is a declaration, not a working
launch path, so the table says which is which:

| `spec.harness` | Registry entry     | Selected by                                       | Process shape                     | `sac agents start`? |
|----------------|--------------------|---------------------------------------------------|-----------------------------------|---------------------|
| `anthropic` *(default)* | `claude-code-tui`  | `runtime: tui`, or unset                  | external `claude` binary in a PTY | **yes** |
| `anthropic`    | `claude-agent-sdk` | `runtime: claude-agent-sdk` (legacy alias `apptainer`) | sac-hosted session runner | **yes** |
| `openai`       | `openai-agents`    | the harness axis alone                            | sac-hosted session runner         | **no** — refused |
| `codex`        | `codex-sdk`        | the harness axis alone                            | sac-hosted session runner         | **no** — refused |

`spec.runtime` only discriminates *within* the `anthropic` family; the `openai`
and `codex` families have one entry each, so the runtime axis selects nothing
for them.

**Honest limit today:** a non-`anthropic` harness loads, validates and resolves
to its registry entry, but every lifecycle launch path *refuses* it — loudly,
rather than silently starting a Claude harness under a spec that asked for
something else. The one working alternative is `spec.a2a.handler:
openai_session` for the OpenAI SDK; there is no equivalent A2A executor for
`codex` yet.

`spec.claude.session` controls whether to start fresh (`new-session`), continue
the last session (`continue`), or resume a specific one (`resume <sid>`) for the
Claude-family harnesses.

### A2A inbound (`spec.a2a.port`)

When `spec.a2a.port` is set, `sac` binds `POST /v1/turn` on that localhost port
for this agent. Any process on the host can send a prompt without knowing the
tmux pane — including other agents via `sac peer post-turn`.

### `sac listen` (control plane)

A per-host REST API (bearer-auth, loopback-only) that exposes fleet-wide
operations: health checks, agent status, send, agent-card, and more.
`sac channel send` routes through it for local agent-to-agent messaging.

## Restart / health

The runner supervisor checks `spec.health` probes and applies `spec.restart`
policy (never / on-failure / always) with exponential backoff.
Heartbeat state is written to `runtime/<name>/heartbeat.json` every tick.

## See also

- [spec-reference.md](spec-reference.md) — full field reference
- [directories.md](directories.md) — directory tree + config cascade
- [images.md](images.md) — Apptainer image management
