# Talking to a Running Agent

A `sac` agent is a long-lived process: once started, it stays alive
and accepts new turns. Three transports reach into that process,
ordered from external-friendliest to most internal:

| Transport                              | When to use                                                                  | Auth      |
|----------------------------------------|------------------------------------------------------------------------------|-----------|
| **A2A** — `POST /v1/turn`              | External tools, browsers, curl, peer agents, A2A-spec consumers              | none (loopback) |
| **CLI** — `sac agents send` / `tail`   | Scripted flows on the same host (the one running the agent)                  | none      |
| **`sac listen`** — `/v1/sac/agents/.../send` | Trusted orchestrators (e.g. orochi), cross-host via the existing SSH mesh    | bearer token |

All three end up dropping a `TurnEnvelope` on the runner's shared
inbox, so the SDK conversation is identical regardless of which door
the prompt came through.

---

## 1. A2A — POST /v1/turn (and `/.well-known/agent-card.json`)

Enable it by setting `spec.a2a.port` in the agent's spec.yaml:

```yaml
spec:
  a2a:
    port: 7901          # any free port; loopback only by default
```

The per-agent sidecar then exposes four endpoints on that port:

| Method | Path                              | Purpose                                              |
|--------|-----------------------------------|------------------------------------------------------|
| POST   | `/v1/turn`                        | Drop a prompt onto the live SDK session              |
| GET    | `/health`                         | `{status: "ok"}` liveness probe                      |
| GET    | `/.well-known/agent-card.json`    | A2A discovery card (built from this agent's `spec.yaml`) |
| GET    | `/.well-known/agent.json`         | Alias of the agent-card path (some clients try this) |

### Send a turn

```bash
curl -s --max-time 120 -X POST http://127.0.0.1:7901/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "Which scitex packages currently have audit-all violations?"}'
```

Response shape:

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
curl -s -X POST http://127.0.0.1:7878/v1/sac/agents/ecosystem-auditor/send \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"prompt","prompt":"Same question, via sac listen."}'
```

The full host-level surface (mirrored at `/v1/a2a/...` for A2A-spec
consumers):

| Method | Path                                         | Purpose                                |
|--------|----------------------------------------------|----------------------------------------|
| GET    | `/v1/sac/health`                             | `{ok, version, host}`                  |
| GET    | `/v1/sac/agents`                             | List all agents on this host           |
| GET    | `/v1/sac/agents/<name>/status`               | Full agent state                       |
| GET    | `/v1/sac/agents/<name>/tail?since=...&follow=...` | SSE stream of `session.jsonl`     |
| POST   | `/v1/sac/agents/<name>/send`                 | Send prompt or interrupt key           |
| GET    | `/v1/sac/agents/<name>/card`                 | AgentCard for this agent               |
| POST   | `/v1/sac/agents`                             | Create + start from an inline spec     |
| DELETE | `/v1/sac/agents/<name>`                      | Stop the agent                         |

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

## Picking a transport

- **Browser or third-party A2A tool** → A2A `POST /v1/turn`.
- **Shell script on the same host** → `sac agents send` + `tail`.
- **Another agent, on the same host** → A2A; agents have `httpx` in the SIF.
- **Orchestrator (orochi, custom)** → `sac listen` with the bearer token; cross-host via the existing mesh.

Pick the most external transport that meets your needs — every layer
above the inbox is a thin wrapper, so there's no functional difference
once the turn lands.

## See also

- [`spec-reference.md`](spec-reference.md) — the YAML knobs (`spec.a2a.port`, `spec.listen.port`)
- [`how-sac-works.md`](how-sac-works.md) — the architecture diagram showing where each transport hooks in
- [`sac-and-orochi.md`](sac-and-orochi.md) — how orochi consumes `sac listen` across hosts
