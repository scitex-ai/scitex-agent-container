# A2A protocol — native sac surface

[A2A](https://a2a-protocol.org/) is an open agent-to-agent JSON-RPC protocol. sac speaks it directly, with **zero fleet dependencies**: no orochi, no Cloudflare tunnel, no Gitea identity. A single agent YAML can expose its own A2A endpoint with one command.

## Why sac knows A2A but not orochi

A2A is a **protocol**; orochi is one **implementation** of a fleet hub on top of A2A. sac knowing A2A doesn't violate the layering — same as a generic HTTP library knowing HTTP without knowing nginx. By making A2A native to sac, a lab can adopt the *protocol* without adopting an entire fleet stack.

Concrete value:

- **Standalone agent deploy** — `sac a2a serve agent.yaml` boots one A2A agent. Done.
- **Protocol-aware health check** — sac health can hit an AgentCard endpoint (future).
- **Swappable fleet implementations** — orochi is one consumer of sac-served A2A endpoints; another fleet hub can be too.

## CLI

```bash
sac a2a serve <agent.yaml>... [--host 127.0.0.1] [--port 8888] [--handler {echo,claude_cli,exec}] [-v]
```

| Handler | What it does | When to use |
| --- | --- | --- |
| `echo` (default) | canned reply with the user text | smoke-test the protocol surface; zero deps |
| `claude_cli` | runs `claude --print` and forwards stdout | real LLM agent; needs `claude` on PATH |
| `exec` | runs `$SAC_A2A_EXEC_COMMAND`, pipes user text on stdin, returns stdout | wire in any custom handler script |

The server exposes the standard A2A routes:

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/.well-known/agent.json` | fleet AgentCard listing all agents in this server |
| GET | `/v1/agents/` | JSON list of agents |
| GET | `/v1/agents/<name>/.well-known/agent.json` | per-agent AgentCard |
| POST | `/v1/agents/<name>` | JSON-RPC `tasks/send` / `tasks/get` |

## Quick verification

```bash
# Boot a standalone echo agent
sac a2a serve my-agent.yaml --port 8888 &

# Discovery
curl http://127.0.0.1:8888/.well-known/agent.json | jq .name

# JSON-RPC tasks/send
curl -s -X POST http://127.0.0.1:8888/v1/agents/<name> \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"t","method":"tasks/send",
       "params":{"message":{"role":"user","parts":[{"type":"text","text":"hello"}]}}}' \
  | jq '.result | {state: .status.state, reply: .history[1].parts[0].text}'
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
| `spec.skills.required` | `skills[0].tags` (merged with capabilities) |
| `spec.host` / `spec.hosts` | `x-scitex-agent-container.scheduling` |
| `spec.runtime` / `model` / `multiplexer` | `x-scitex-agent-container.*` |

sac-specific extensions live under **`x-scitex-agent-container`**, NOT `x-orochi`. The orochi extension namespace is owned by that project and would couple sac to it; keeping them separate is the whole point.

## Handler env vars

| Env var | Default | Read by |
| --- | --- | --- |
| `SAC_A2A_CLAUDE_BIN` | `claude` | `claude_cli` handler |
| `SAC_A2A_CLAUDE_MODEL` | (unset → CLI default) | `claude_cli` handler |
| `SAC_A2A_CLAUDE_SYSTEM` | a "be brief, no tools" prompt | `claude_cli` handler |
| `SAC_A2A_CLAUDE_TIMEOUT_S` | 25 | `claude_cli` handler |
| `SAC_A2A_EXEC_COMMAND` | (required) | `exec` handler — full `argv` (shell-quoted) |
| `SAC_A2A_EXEC_TIMEOUT_S` | 25 | `exec` handler |

## Boundary with orochi

orochi (the fleet hub) is **one consumer** of sac-served A2A endpoints. Its dispatch bridge (`https://a2a.scitex.ai/v1/agents/<n>` → `https://scitex-orochi.com/api/a2a/dispatch/...` → WebSocket-connected agent) routes through a hub that knows about workspaces, bearer auth via Gitea, and Channels groups. None of that is required for sac's A2A — those are orochi-side features layered on top.

If you want a fleet, use orochi. If you want one agent on a laptop, use `sac a2a serve`.

## Cross-references

- [`06_env-injection-ports.md`](06_env-injection-ports.md) — the four env-injection ports (yaml.env / src_mcp.json env / src_env / hooks)
- [scitex-orochi `docs/a2a-protocol.md`](https://github.com/ywatanabe1989/scitex-orochi/blob/develop/docs/a2a-protocol.md) — fleet-side architecture (Tier 3 dispatch bridge)
