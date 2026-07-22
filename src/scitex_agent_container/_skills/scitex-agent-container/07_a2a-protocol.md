---
description: |
  [TOPIC] A2A protocol — native sac surface
  [DETAILS] CLI (`sac a2a serve` / `doctor`), auto-launch via `spec.a2a`, SDK 1.x methods, handler env vars, fleet-hub boundary. AgentCard extension fields live in the sibling leaf.
tags: [scitex-agent-container-a2a-protocol]
---

# A2A protocol — native sac surface

[A2A](https://a2a-protocol.org/) is an open agent-to-agent JSON-RPC protocol. sac speaks it directly, with **zero fleet dependencies**: no fleet hub, no Cloudflare tunnel, no Gitea identity. A single agent YAML can expose its own A2A endpoint with one command.

The per-card `x-scitex-agent-container.*` extension fields and a full JSON example live in [`07_a2a-protocol-extension-fields.md`](07_a2a-protocol-extension-fields.md).

## Why sac knows A2A but not any fleet hub

A2A is a **protocol**; a fleet hub is one **implementation** on top of A2A. sac knowing A2A doesn't violate the layering — same as a generic HTTP library knowing HTTP without knowing nginx. By making A2A native to sac, a lab can adopt the *protocol* without adopting an entire fleet stack.

Concrete value:

- **Standalone agent deploy** — `sac a2a serve agent.yaml` boots one A2A agent. Done.
- **Protocol-aware health check** — sac agents health can hit an AgentCard endpoint (future).
- **Swappable fleet implementations** — any fleet hub is one consumer of sac-served A2A endpoints; another can be too.

## CLI

```bash
sac a2a serve  <agent.yaml>... [--host 127.0.0.1] [--port 8888] [--handler {echo,claude_cli,exec}] [-v]
sac a2a doctor <agent.yaml>    [--host H] [--port N] [--timeout 5.0] [--json]
```

`a2a doctor` GETs the AgentCard endpoint declared by `spec.a2a` and reports liveness + round-trip latency. Exit codes: `0` healthy, `1` unhealthy/unreachable, `2` config error (no `spec.a2a.port`).

## Auto-launch via `spec.a2a`

When a v3 YAML declares `spec.a2a.port`, `sac agents start` spawns the A2A server as a sidecar subprocess after the multiplexer is up. PID lives at `{workdir}/a2a-sidecar.pid`, output at `{workdir}/a2a-sidecar.log`; `sac agents stop` SIGTERMs it via the PID file.

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  name: my-agent
spec:
  runtime: apptainer
  a2a:
    port: 8888
    handler: echo          # echo (default) | claude_cli | exec
    host: 127.0.0.1        # default; use 0.0.0.0 to expose externally
```

Disabled by default — the sidecar only starts when `spec.a2a` is present. Sidecar failures are logged and swallowed; agent start/stop is never blocked by A2A.

| Handler | What it does | When to use |
| --- | --- | --- |
| `echo` (default) | canned reply with the user text | smoke-test the protocol surface; zero deps |
| `claude_cli` | runs `claude --print` and forwards stdout | real LLM agent; needs `claude` on PATH |
| `exec` | runs `$SAC_A2A_EXEC_COMMAND`, pipes user text on stdin, returns stdout | wire in any custom handler script |

## HTTP routes

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/.well-known/agent-card.json` | fleet AgentCard listing all agents in this server |
| GET | `/agents/` | JSON list of agents |
| GET | `/agents/<name>/.well-known/agent-card.json` | per-agent AgentCard (sac dict shape; `x-scitex-agent-container` extension preserved) |
| POST | `/agents/<name>/message:send` | A2A v1 REST binding — JSON-RPC SDK 1.x methods (see below) |
| GET | `/agents/<name>/inbox/stream` | sac extension — SSE stream of inbound events (consumed by `sac mcp channel`) |
| GET | `/agents/<name>/_active` | sac extension — observability snapshot of in-memory tasks |

A2A v1.0 renamed the well-known file from `agent.json` (v0.x) to `agent-card.json`. sac serves the v1 path only; the v0 path is **not** backed by a compatibility shim. See [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md).

## SDK 1.x methods (gRPC-style names)

Pure `a2a-sdk>=1.0.2` — no v0.3 compat. Method names are gRPC-style:

| Method | Purpose |
| --- | --- |
| `SendMessage` | unary task dispatch — synchronous reply |
| `SendStreamingMessage` | SSE-streamed task — incremental progress + artifacts |
| `GetTask` | poll task by id |
| `CancelTask` | interrupt a running task |
| `pushNotificationConfig/*` | webhook subscription |

Clients MUST set `A2A-Version: 1.0` header. Params use proto **snake_case** (`message_id`, `role: "ROLE_USER"`, `parts: [{"text": ...}]`).

## Quick verification

```bash
# Boot a standalone echo agent
sac a2a serve my-agent.yaml --port 8888 &

# Discovery (v1 path; agent.json is v0.x and no longer served)
curl http://127.0.0.1:8888/.well-known/agent-card.json | jq .name

# JSON-RPC SendMessage (SDK 1.x) — POSTed to the A2A v1 REST binding
curl -s -X POST http://127.0.0.1:8888/agents/<name>/message:send \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"t","method":"SendMessage",
       "params":{"message":{"message_id":"m1","role":"ROLE_USER",
                            "parts":[{"text":"hello"}]}}}' \
  | jq '.result.task | {state: .status.state, reply: .status.message.parts[0].text}'

# SSE streaming
curl -N -X POST http://127.0.0.1:8888/agents/<name>/message:send \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -H 'Accept: text/event-stream' \
  -d '{"jsonrpc":"2.0","id":"t","method":"SendStreamingMessage",
       "params":{"message":{"message_id":"m1","role":"ROLE_USER",
                            "parts":[{"text":"long-running"}]}}}'
```

## Handler env vars

| Env var | Default | Read by |
| --- | --- | --- |
| `SAC_A2A_CLAUDE_BIN` | `claude` | `claude_cli` handler |
| `SAC_A2A_CLAUDE_MODEL` | (unset → CLI default) | `claude_cli` handler |
| `SAC_A2A_CLAUDE_SYSTEM` | a "be brief, no tools" prompt | `claude_cli` handler |
| `SAC_A2A_CLAUDE_TIMEOUT_S` | 25 | `claude_cli` handler |
| `SAC_A2A_EXEC_COMMAND` | (required) | `exec` handler — full `argv` (shell-quoted) |
| `SAC_A2A_EXEC_TIMEOUT_S` | 25 | `exec` handler |

## Implementation — `a2a-sdk` 1.x

sac uses the official Python `a2a-sdk[http-server]>=1.0.2`. Handlers are `AgentExecutor` subclasses (`a2a/executors/{_echo,_claude_cli,_exec}.py`) with `async execute(context, event_queue)` that enqueues a `Task` (state `SUBMITTED`), drives status updates, and emits `TaskArtifactUpdateEvent` / `TaskStatusUpdateEvent` events. The SDK handles dispatch routing, SSE serialization, and task store wiring.

### Known compatibility notes

- **`protobuf<7` required**: a2a-sdk 1.0.2 reads `FieldDescriptor.label` which protobuf 7.x removed. Pinned in deps.
- **`uvicorn ws="none"`**: A2A is HTTP+SSE only — uvicorn 0.27's WS protocol auto-loader breaks on websockets 15.x (`websockets.legacy` removed). Sac passes `ws="none"` so the sidecar boots cleanly.
- **AgentCard is protobuf**: SDK 1.x expects a protobuf `AgentCard`, not pydantic dict. `_card.project_card_proto()` is the adapter; the dict form (`project_card()`) is what gets served at `/.well-known/agent-card.json` (v1 name).

## Boundary with orochi

orochi (the fleet hub) is **one consumer** of sac-served A2A endpoints. Its dispatch bridge serves the same SDK 1.x surface at `https://scitex-orochi.com/agents/<name>/` and proxies into the live agent's sidecar (Tier-3 HTTP-direct, or WS fallback). orochi adds workspace-token auth (`WorkspaceTokenContextBuilder`), agent registry resolution, and chat-room semantics on top. None of that is required for sac's A2A — those are orochi-side features layered on top.

If you want a fleet, use orochi. If you want one agent on a laptop, use `sac a2a serve`.

## Cross-references

- [`07_a2a-protocol-extension-fields.md`](07_a2a-protocol-extension-fields.md) — `x-scitex-agent-container.*` per-agent / fleet / proxy field enumeration + full JSON card example
- [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md) — A2A v1.0 compliance
- [`06_env-injection-ports.md`](06_env-injection-ports.md) — the four env-injection ports (yaml.env / to_home/.mcp.json env / to_home/.env / hooks)
- [scitex-orochi `docs/a2a-protocol.md`](https://github.com/ywatanabe1989/scitex-orochi/blob/develop/docs/a2a-protocol.md) — fleet-side architecture (Tier 3 dispatch bridge)
