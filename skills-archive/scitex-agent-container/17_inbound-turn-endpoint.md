---
description: |
  [TOPIC] Inbound-turn HTTP endpoint (`POST /v1/turn`)
  [DETAILS] Reference for the in-runner HTTP inbound-turn endpoint served by the in-container claude-session SDK runner when ``spec.a2a.port`` is declared. Wire format, semantics, curl examples, and how it differs from the legacy A2A sidecar. Runs inside the apptainer SIF — `runtime: apptainer` is the operative runtime.
tags: [scitex-agent-container-inbound-turn-endpoint, claude-session, a2a, inbound]
---

# Inbound-turn HTTP endpoint (`POST /v1/turn`)

Long-living agents accept new turns over HTTP. The endpoint is
**colocated with the SDK conversation** (no separate sidecar process) so
each turn lands on the same persistent `ClaudeSDKClient` — the resume id,
accumulated quota, and tool history are preserved across turns.

The endpoint lives in `_runners/_session_http.py`, spawned by the SDK
runner (`_runners/claude_session.py::run`) when its argv carries
`--a2a-port N`. The runner — and therefore this HTTP server — runs
**inside the apptainer container** (`apptainer exec`); the runtime adapter
sets `--a2a-port` from `spec.a2a.port`. See
[15_claude-session.md](15_claude-session.md) for the container shape.

## YAML

`runtime: apptainer` is the only accepted runtime (sac is apptainer-only;
the SDK runner is invoked inside the SIF). Enable the endpoint with
`spec.a2a.port`:

```yaml
spec:
  runtime: apptainer
  apptainer:
    image: /home/me/.scitex/agent-container/containers/sac-base.sif
    relaxed: true
  a2a:
    port: 18888         # int, or "auto" (default) — set to enable inbound HTTP
    host: 127.0.0.1     # default; set to 0.0.0.0 for LAN exposure
```

`port: auto` (the default) lets sac allocate; clients then reach the agent
through `sac listen` (one host port, name-in-path) rather than the
per-agent port directly. Pin an int to bind a fixed port.

## Wire format

```bash
# One turn → one assistant reply
curl -sX POST http://127.0.0.1:18888/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "summarize today commits", "exit_after": false}'
# 200 → {"reply": "...", "exit_after": false}

# Tell the runner to shut down after this turn (CI smokes use this)
curl -sX POST http://127.0.0.1:18888/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "echo done", "exit_after": true}'

# Liveness
curl -s http://127.0.0.1:18888/health
# 200 → {"status": "ok"}
```

## Semantics

- **Serial**, not interleaved. A new POST waits until the prior turn's `receive_response()` drains. Matches Claude Code's own UX (next prompt waits).
- **Per-turn timeout: 600 s.** SDK hangs surface as `504` with `{"error": "turn timeout after 600s"}`.
- **Validation:** missing or empty `text` → `400`.
- **Errors:** SDK runtime errors surface as `502` with `{"error": "turn failed: <detail>"}` and the same envelope is appended to `session.jsonl` (kind: `sdk_runtime`).

## How it differs from the legacy A2A sidecar

The in-runner endpoint replaced the standalone A2A sidecar that the old
CLI/TUI runtime used. For SDK agents (`kind: Agent`) the runner hosts
`/v1/turn` itself; the `sac a2a serve` sidecar path is retained only for
non-SDK runtimes (see [07_a2a-protocol.md](07_a2a-protocol.md)).

| Aspect | Legacy `sac a2a serve` sidecar | In-runner (current) |
|---|---|---|
| Process | Separate `sac a2a serve` process | Asyncio task inside the SDK runner (in-container) |
| Per-request transport | New `query()` per request — fresh conversation each time | `client.query()` on the persistent SDK client — turns share context |
| Wire | A2A JSON-RPC `message/send` | Plain `{text, exit_after}` |
| Concurrency | Each request spawns its own SDK call | Serial drain — turns queue |
| Resume | None (each request is stateless) | Full — session_id persists across runs |

## Wiring details

The runner's argv `--a2a-port`/`--a2a-host` is set automatically by `runtimes/claude_session.py::start` from `spec.a2a.port` / `spec.a2a.host`. The handler enqueues a `TurnEnvelope` on the runner's `asyncio.Queue` and awaits `env.response`; the conversation task drains it, calls `client.query(text)`, drains `receive_response()`, and resolves the future.

## Implementation files

- `src/scitex_agent_container/_runners/_session_http.py` — Starlette app + uvicorn task
- `src/scitex_agent_container/_runners/_session_inbox.py` — `TurnEnvelope` / `ShutdownEnvelope` / `make_inbox()`
- `src/scitex_agent_container/_runners/claude_session.py::_run_conversation` — drains the inbox into the persistent `ClaudeSDKClient`
- `tests/scitex_agent_container/_runners/test__session_http.py` — round-trip + 400 + health smoke tests

## Cross-host placement

The old host-side bare-Python launch (a `render_remote_launch` bash-script
generator + `SAC_RUNNER_PREFIX` wrapper, exec'd over ssh) was **removed**
with the bare-metal/SSH-dispatch ripout (WI-6, 2026-05-20). The runner
only ever launches via `apptainer exec` now; there is no host-side
`python -m ... claude_session` path.

> Note: `_runners/_remote_launch.render_remote_launch` still exists as a
> module with a unit test, but it is **not wired into any live launch
> path** (no importers in `src/` outside its own test). Treat it as dead
> code, not as the way agents start on remote hosts.

Cross-host work goes through two mechanisms:

- **`spec.host`** — pin an agent to a host (or a priority list). See
  [11_remote-deploy.md](11_remote-deploy.md).
- **`sac --on <peer>`** (F-CS12) — dispatch a `sac` command on a peer
  defined in `config.yaml`'s `peers:` block.

### Reaching a remote agent's `/v1/turn` — ssh-as-transport

The runner binds `/v1/turn` on `127.0.0.1` inside its container — never on
a LAN interface. A peer reaches a remote agent through ssh, resolved
automatically by the peer helper:

```python
from scitex_agent_container.peer import post_turn
reply = post_turn("head-mba", "your message")
# resolves to ssh://mba:18888/v1/turn → ssh + curl on the remote loopback
```

This works for ssh aliases that aren't DNS-resolvable from the caller
(e.g. `mba`, `head-spartan`) and survives NAT, while keeping the endpoint
loopback-only. See [06_http-api.md](06_http-api.md) and
[07_a2a-protocol.md](07_a2a-protocol.md).
