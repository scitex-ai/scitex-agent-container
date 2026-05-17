---
description: |
  [TOPIC] A2A protocol — native sac surface
  [DETAILS] A2A protocol — native sac surface — see file body for details..
tags: [scitex-agent-container-a2a-protocol]
---

# A2A protocol — native sac surface

[A2A](https://a2a-protocol.org/) is an open agent-to-agent JSON-RPC protocol. sac speaks it directly, with **zero fleet dependencies**: no orochi, no Cloudflare tunnel, no Gitea identity. A single agent YAML can expose its own A2A endpoint with one command.

## Why sac knows A2A but not orochi

A2A is a **protocol**; orochi is one **implementation** of a fleet hub on top of A2A. sac knowing A2A doesn't violate the layering — same as a generic HTTP library knowing HTTP without knowing nginx. By making A2A native to sac, a lab can adopt the *protocol* without adopting an entire fleet stack.

Concrete value:

- **Standalone agent deploy** — `sac a2a serve agent.yaml` boots one A2A agent. Done.
- **Protocol-aware health check** — sac agent health can hit an AgentCard endpoint (future).
- **Swappable fleet implementations** — orochi is one consumer of sac-served A2A endpoints; another fleet hub can be too.

## CLI

```bash
sac a2a serve  <agent.yaml>... [--host 127.0.0.1] [--port 8888] [--handler {echo,claude_cli,exec}] [-v]
sac a2a doctor <agent.yaml>    [--host H] [--port N] [--timeout 5.0] [--json]
```

`a2a doctor` GETs the AgentCard endpoint declared by `spec.a2a` and reports liveness + round-trip latency. Exit codes: `0` healthy, `1` unhealthy/unreachable, `2` config error (no `spec.a2a.port`).

## Auto-launch via `spec.a2a`

When a v3 YAML declares `spec.a2a.port`, `sac agent start` spawns the A2A server as a sidecar subprocess after the multiplexer is up. PID lives at `{workdir}/a2a-sidecar.pid`, output at `{workdir}/a2a-sidecar.log`; `sac agent stop` SIGTERMs it via the PID file.

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  name: my-agent
spec:
  runtime: claude-code
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

The server exposes the standard A2A routes:

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/.well-known/agent-card.json` | fleet AgentCard listing all agents in this server |
| GET | `/agents/` | JSON list of agents |
| GET | `/agents/<name>/.well-known/agent-card.json` | per-agent AgentCard (sac dict shape; `x-scitex-agent-container` extension preserved) |
| POST | `/agents/<name>/message:send` | A2A v1 REST binding — JSON-RPC SDK 1.x methods (see below) |
| GET | `/agents/<name>/inbox/stream` | sac extension — SSE stream of inbound events (consumed by `sac mcp channel`) |
| GET | `/agents/<name>/_active` | sac extension — observability snapshot of in-memory tasks |

A2A v1.0 renamed the well-known file from `agent.json` (v0.x) to
`agent-card.json`. sac serves the v1 path only; the v0 path is **not**
backed by a compatibility shim. See [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md).

### SDK 1.x methods (gRPC-style names)

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

## v3 YAML — what gets projected

Any v3 sac YAML works. The projection reads:

| Field | Mapped to AgentCard |
| --- | --- |
| `metadata.name` (or filename stem) | `name` |
| `metadata.labels.capabilities` (CSV) | first item → `description`; all items → `skills[0].tags` |
| `metadata.labels.team` | `provider.organization` |
| `metadata.labels.role` | `skills[0].name`, `x-scitex-agent-container.role_class` |
| `metadata.labels.function` (CSV) | `skills[0].description` |
| `metadata.labels.skills` (CSV) | `skills[0].tags` ∪ `x-scitex-agent-container.required_skills` |
| `spec.host` / `spec.hosts` | `x-scitex-agent-container.scheduling` |
| `spec.runtime` / `claude.model` / `multiplexer` | `x-scitex-agent-container.runtime` / `.model` / `.multiplexer` |
| `spec.apptainer.*` | `x-scitex-agent-container.isolation.*` (D3 attestation block) |
| `spec.claude.channels: [server:sac]` | `capabilities.extensions[]` (sac-push-channel/v1) |

## sac extension namespace (`x-scitex-agent-container.*`)

A2A v1.0 reserves the AgentCard top level for spec-defined fields and funnels vendor data into a namespaced extension block. **sac uses exactly one namespace key: `x-scitex-agent-container`.** Every sac-specific datum lives under that key, never at the top level, never under another vendor namespace (e.g. `x-orochi` is owned by the orochi fleet hub, not by sac).

This contract is what makes sac-served cards forward-compatible with vendor-neutral A2A clients — strict v1 validators (`ParseDict(AgentCard)`) ignore the `x-*` namespace; sac-aware clients walk into it. See [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md) for the rule.

### Per-agent card fields

Emitted by `a2a/_card.py::project_card`. Every per-agent card served at `GET /agents/<name>/.well-known/agent-card.json` carries:

| Field | Source | Description |
| --- | --- | --- |
| `x-scitex-agent-container.role_class` | `metadata.labels.role` | Operator-declared role taxonomy (e.g. `worker-telegrammer`). Mirrors `skills[0].name`. |
| `x-scitex-agent-container.cardinality` | `metadata.labels.cardinality` | `singleton` / `multi-instance` hint for fleet schedulers. |
| `x-scitex-agent-container.scheduling` | `spec.host` / `spec.hosts` | `{mode, priority|hosts}` placement hint. |
| `x-scitex-agent-container.runtime` | `spec.runtime` | Runtime kind (`claude-code`, `agent-proxy`, etc.). |
| `x-scitex-agent-container.model` | `spec.claude.model` ∨ `spec.model` (legacy) | LLM model identifier. |
| `x-scitex-agent-container.multiplexer` | `spec.multiplexer` | tmux / zellij / none. |
| `x-scitex-agent-container.required_skills` | `metadata.labels.skills` ∪ `spec.skills.required` | Skill IDs the agent loads at boot. |
| `x-scitex-agent-container.isolation` | derived from `spec.apptainer.*` | D3 attestation block — `{level, containall, cleanenv, writable_tmpfs, preflight_passed, preflight_allowed, binds_count, binds_writable_count}`. External attestation surfaces (Clew, orochi) read these booleans. |

### Per-agent `capabilities.extensions[]` entries

The A2A v1 spec-defined `capabilities.extensions[]` array advertises sac extensions by URI (distinct from `x-scitex-agent-container`):

| URI | Emitted when | Purpose |
| --- | --- | --- |
| `https://scitex.ai/a2a/extensions/sac-push-channel/v1` | `spec.claude.channels` contains `server:sac` | In-session MCP push: `sac mcp channel` SSE-subscribes to `/agents/<name>/inbox/stream` and forwards events as `notifications/claude/channel` to the agent's Claude session. `params.sse_path` + `params.mcp_tools` enumerate wire details. |

### Fleet card fields

Emitted by `a2a/_card.py::fleet_card` at `GET /.well-known/agent-card.json`:

| Field | Type | Description |
| --- | --- | --- |
| `x-scitex-agent-container.agents` | list[object] | Member directory. Each entry has `name` + `supportedInterfaces[]`. Spec-aware clients walk this array to fetch each member's per-agent card. |

Plus the fleet-level `capabilities.extensions[]`:

| URI | Purpose |
| --- | --- |
| `https://scitex.ai/a2a/extensions/sac-fleet/v1` | Declares the multi-agent directory shape. `params.members_path` + `params.member_card_path` tell vendor-neutral clients how to walk the fleet. |

### AgentProxy overlay (`kind: AgentProxy`)

Emitted by `_runners/a2a_proxy.py::splice_card` when an AgentProxy runner serves its card:

| Field | Source | Description |
| --- | --- | --- |
| `x-scitex-agent-container.kind` | runner constant | `"AgentProxy"` — distinguishes a proxy from a native sac runtime. |
| `x-scitex-agent-container.upstream` | `--upstream` CLI arg | Upstream A2A URL the proxy forwards to. |
| `x-scitex-agent-container.trust` | `--trust` CLI arg | Trust tier (`trusted` / `untrusted`). |
| `x-scitex-agent-container.upstream_card_fetch_error` | runtime | Present only when boot-time fetch of the upstream card failed. |

### Concrete example

Per-agent card served at `GET /agents/my-agent/.well-known/agent-card.json`:

```json
{
  "name": "my-agent",
  "description": "sac agent: my-agent (worker-telegrammer)",
  "version": "scitex-agent-container/v3",
  "supportedInterfaces": [
    {
      "url": "http://127.0.0.1:8888/agents/my-agent",
      "protocolBinding": "HTTP+JSON",
      "tenant": "my-agent",
      "protocolVersion": "1.0"
    }
  ],
  "provider": {"organization": "scitex-agent-container", "url": "https://scitex.ai"},
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "extendedAgentCard": false,
    "extensions": [
      {
        "uri": "https://scitex.ai/a2a/extensions/sac-push-channel/v1",
        "description": "In-session MCP push: sac mcp channel subscribes ...",
        "required": false,
        "params": {
          "sse_path": "/agents/my-agent/inbox/stream",
          "mcp_tools": ["a2a_send", "a2a_reply", "a2a_ack", "a2a_peers", "a2a_inbox"]
        }
      }
    ]
  },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {
      "id": "my-agent.worker-telegrammer",
      "name": "worker-telegrammer",
      "description": "relays telegram messages",
      "tags": ["a2a", "telegram"]
    }
  ],
  "x-scitex-agent-container": {
    "role_class": "worker-telegrammer",
    "cardinality": "singleton",
    "scheduling": {"mode": "singleton", "priority": ["ywata-note-win"]},
    "runtime": "claude-code",
    "model": "claude-opus-4-7",
    "multiplexer": "tmux",
    "required_skills": ["quality-guards", "autonomous", "speech", "scitex"],
    "isolation": {
      "level": "hardened",
      "containall": true,
      "cleanenv": true,
      "writable_tmpfs": true,
      "preflight_passed": ["uid-nonzero", "no-host-home"],
      "preflight_allowed": [],
      "binds_count": 3,
      "binds_writable_count": 1
    }
  }
}
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

- [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md) — A2A v1.0 compliance + the authoritative `x-scitex-agent-container.*` field enumeration this doc mirrors
- [`06_env-injection-ports.md`](06_env-injection-ports.md) — the four env-injection ports (yaml.env / dot_claude/.mcp.json env / dot_claude/.env / hooks)
- [scitex-orochi `docs/a2a-protocol.md`](https://github.com/ywatanabe1989/scitex-orochi/blob/develop/docs/a2a-protocol.md) — fleet-side architecture (Tier 3 dispatch bridge)
- Implementation source of truth: `a2a/_card.py::project_card`, `a2a/_card.py::fleet_card`, `_runners/a2a_proxy.py::splice_card`
