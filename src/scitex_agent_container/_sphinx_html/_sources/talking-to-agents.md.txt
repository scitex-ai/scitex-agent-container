<!-- ---
!-- Timestamp: 2026-05-13 19:55:42
!-- Author: ywatanabe
!-- File: /home/ywatanabe/proj/scitex-agent-container/docs/talking-to-agents.md
!-- --- -->

# Talking to a Running Agent

A `sac` agent is a long-lived process: once started, it stays alive
and accepts new turns. Three transports reach into that process,
ordered from external-friendliest to most internal:

| Transport                                                                    | When to use                                                               | Auth            |
|------------------------------------------------------------------------------|---------------------------------------------------------------------------|-----------------|
| **A2A sidecar** — `POST /agents/<name>/turn` on the agent's own port  | External tools, browsers, curl, peer agents, A2A-spec consumers           | none (loopback) |
| **CLI** — `sac agents send` / `tail`                                         | Scripted flows on the same host (the one running the agent)               | none            |
| **`sac listen`** — `POST /agents/<name>/send` on the host port (7878) | Trusted orchestrators (e.g. orochi), cross-host via the existing SSH mesh | bearer token    |

All three end up dropping a `TurnEnvelope` on the runner's shared
inbox, so the SDK conversation is identical regardless of which door
the prompt came through.

---

## 1. A2A sidecar — `POST /agents/<name>/turn`

Enable it by leaving `spec.a2a.port` at its default (`auto`) — sac
claims a free port from `~/.scitex/agent-container/config.yaml`'s
`a2a.port_range` (default `[19000, 19999]`) at start time and persists
it in `state.db`. Operators see the assigned port via `sac agents list`.
Most agents need no `a2a` block at all:

```yaml
spec:
  # a2a:
  #   port: auto         # the default; sac picks + persists
  #   port: 7901         # pin only when you need a stable external URL
  #   port: null         # disable the sidecar entirely
```

**Per-agent ports are an internal IPC detail.** External clients reach
every agent through the **one stable host port** at `sac listen`
(default `127.0.0.1:7878`). The AgentCard's `url` field advertises
that stable URL — so the URL survives restarts even when the
per-agent port churns.

The per-agent sidecar then exposes the **same URL shape** as the
host-level `sac listen`, so the same client URL works whether it
routes through the host control plane or POSTs directly to the
agent's port. Per the sac/orochi contract, two symmetric namespaces
serve identical handlers:

| Method | Path                                          | Purpose                                              |
|--------|-----------------------------------------------|------------------------------------------------------|
| POST   | `/agents/<name>/turn`                  | **Canonical.** Drop a prompt onto the SDK session    |
| POST   | `/agents/<name>/send`                  | Alias of `/turn` (matches `sac listen`'s verb)       |
| GET    | `/agents/<name>/card`                  | This agent's AgentCard                               |
| POST   | `/v1/a2a/agents/<name>/turn`                  | A2A-protocol-compat mirror of `/turn`                |
| POST   | `/v1/a2a/agents/<name>/send`                  | A2A-protocol-compat mirror of `/send`                |
| GET    | `/v1/a2a/agents/<name>/card`                  | A2A-protocol-compat mirror of `/card`                |
| POST   | `/v1/turn`                                    | Bare shortcut (port already pins the agent)          |
| GET    | `/.well-known/agent-card.json`                | A2A discovery card (built from this agent's `spec.yaml`) |
| GET    | `/.well-known/agent.json`                     | Alias of `agent-card.json` (some clients try this)   |
| GET    | `/health`                                     | `{status: "ok"}` liveness probe                      |

The `{name}` segment is informational — port routing has already pinned
the request to one agent, so a name mismatch returns `404 {"error":
"this port serves agent 'X', not 'Y'"}` as a sanity check.

### Send a turn

```bash
curl -s --max-time 120 -X POST \
  http://127.0.0.1:7901/agents/ecosystem-auditor/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "Which scitex packages currently have audit-all violations?"}'
```

Or, since the port already identifies the agent, the bare shortcut:

```bash
curl -s --max-time 120 -X POST http://127.0.0.1:7901/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "Same question, via the shortcut."}'
```

Response shape (either path):

```json
{ "reply": "scitex-stats has 2 PS-204 violations; rest are green.",
  "exit_after": false }
```

The runner stays attached after — subsequent POSTs reach the **same**
SDK session (the conversation accumulates).

### Discover the agent

The AgentCard at `/.well-known/agent-card.json` is auto-generated from
the agent's `spec.yaml` (see [`spec-reference.md`](spec-reference.md)
for the field-to-card mapping). Browsers and A2A-spec discovery clients
read it directly:

```bash
curl -s http://127.0.0.1:7901/.well-known/agent-card.json | python3 -m json.tool
```

The card's `url` field advertises `<base>/agents/<name>` —
exactly the path the sidecar serves — so a client following the
advertised URL hits a working endpoint.

### One-shot vs follow-up

The HTTP body accepts an `"exit_after"` flag:

```json
{ "text": "Reply DONE and exit.", "exit_after": true }
```

When `true`, the runner shuts down after this turn. Default is `false`
(stay alive for more turns).

---

## 2. CLI — `sac agents send` / `sac agents tail`

The same-host, no-network path:

```bash
# Send a turn — output is just the ack; the reply lands on session.jsonl
sac agents send ecosystem-auditor "Which packages have uncommitted changes?"

# Read the latest assistant turns
sac agents tail ecosystem-auditor -n 5

# Or stream as structured JSON (one envelope per line)
sac agents tail ecosystem-auditor -n 5 --json
```

This is the right transport for shell scripts driving an agent
locally — no port, no JSON, no token.

---

## 3. `sac listen` — host-level HTTP control plane

`sac listen` boots a host-wide bearer-auth HTTP server (default port
`7878`, loopback only). Cross-host orchestrators reach it through the
existing SSH mesh; same-host orchestrators speak to it directly.

```bash
# Start the listen server (one per host; sac respects existing instance)
sac listen &

TOKEN=$(cat ~/.scitex/agent-container/tokens/listen-$(hostname).token)

# Send a turn through the control plane
curl -s -X POST http://127.0.0.1:7878/agents/ecosystem-auditor/send \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"prompt","prompt":"Same question, via sac listen."}'
```

The full host-level surface (mirrored at `/v1/a2a/...` for A2A-spec
consumers):

| Method | Path                                         | Purpose                                |
|--------|----------------------------------------------|----------------------------------------|
| GET    | `/v1/sac/health`                             | `{ok, version, host}`                  |
| GET    | `/agents`                             | List all agents on this host           |
| GET    | `/agents/<name>/status`               | Full agent state                       |
| GET    | `/agents/<name>/tail?since=...&follow=...` | SSE stream of `session.jsonl`     |
| POST   | `/agents/<name>/send`                 | Send prompt or interrupt key           |
| GET    | `/agents/<name>/card`                 | AgentCard for this agent               |
| POST   | `/agents`                             | Create + start from an inline spec     |
| DELETE | `/agents/<name>`                      | Stop the agent                         |

`send` accepts both turn types:

```jsonc
// prompt turn
{ "type": "prompt", "prompt": "Your question here", "options": { ... } }

// key / interrupt
{ "type": "key", "key": "ESC" }
```

---

## Cross-host: `sac --on <peer>`

`sac --on <peer> agents send ...` dispatches the call across hosts via
the peer registry's SSH mesh. The remote `sac` does the local send;
output streams back. Same prompt-text contract.

```bash
sac --on gpu-box agents send researcher "Resume training and tail the logs."
```

---

## Forwarding to external A2A (`kind: AgentProxy`)

Sometimes the agent on the other end isn't a sac agent — it's a
peer A2A endpoint hosted somewhere else (a hosted service, a peer
fleet, a contracted vendor). Wrapping it in a `kind: AgentProxy`
agent lets the rest of sac (`sac agents send`, `sac listen`, the
AgentCard discovery surface) treat it the same as a local SDK agent.

The proxy agent has no Claude SDK; it just forwards `POST /v1/turn`
to its configured `spec.proxy.upstream` and re-projects the
upstream AgentCard at its own `/.well-known/agent-card.json` so
peers see one consistent skill list.

```yaml
apiVersion: scitex-agent-container/v3
kind: AgentProxy

spec:
  runtime: apptainer
  apptainer: { image: ~/.scitex/agent-container/containers/sac-proxy.sif }
  proxy:
    upstream: https://peer.example.com
    trust: local-mesh
    redact: [ANTHROPIC_API_KEY, sk-]
    timeout_s: 30.0
  a2a: { port: 7905 }
```

See [`spec-reference.md` § `kind: AgentProxy`](spec-reference.md#kind-agentproxy--http-forwarder-agents)
and [`examples/agents/proxy-agent/`](../examples/agents/proxy-agent/)
for the full reference.

## Picking a transport

- **A2A-spec client / browser / curl** → POST to the URL the AgentCard advertises (`<base>/agents/<name>/turn`). Loopback only; no auth.
- **Shell script on the same host** → `sac agents send` + `tail`. No port, no JSON, no token.
- **Another agent, on the same host** → A2A sidecar; agents have `httpx` in the SIF. Use the canonical URL or the `/v1/turn` shortcut — both work.
- **Orchestrator (orochi, custom)** → `sac listen` (port 7878) with the bearer token; same `/agents/<name>/...` path shape, cross-host via the existing SSH mesh.
- **External A2A peer (hosted service, vendor)** → wrap as a [`kind: AgentProxy`](spec-reference.md#kind-agentproxy--http-forwarder-agents) agent so the rest of sac treats it the same.

Pick the most external transport that meets your needs — every layer
above the inbox is a thin wrapper, so there's no functional difference
once the turn lands. The URL shape is identical on both the per-agent
sidecar and `sac listen`, so the same client code works against either.

## See also

- [`spec-reference.md`](spec-reference.md) — the YAML knobs (`spec.a2a.port`, `spec.listen.port`)
- [`how-sac-works.md`](how-sac-works.md) — the architecture diagram showing where each transport hooks in
- [`sac-and-orochi.md`](sac-and-orochi.md) — how orochi consumes `sac listen` across hosts

<!-- EOF -->