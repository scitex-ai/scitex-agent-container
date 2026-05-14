# sac and orochi

`scitex-agent-container` (`sac`) and [`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi)
are two separate packages with a **one-way dependency**: orochi reads from sac; sac never imports orochi.

## Architecture

```
            ┌────────────────────┐                       ┌──────────────────────┐
            │   Human operator   │  chat · DM · channel  │ claude-code-         │
            │   (web UI / CLI)   │ ◄───── alerts ─────── │ telegrammer          │
            └─────────┬──────────┘                       │ Telegram MCP + TUI   │
                      │                                  └──────────▲───────────┘
                      ▼                                             │
        ┌──────────────────────────────────┐                        │
        │   scitex-orochi                  │                        │
        │   WebSocket hub · dashboard      │                        │
        │   MCP channels · presence · A2A  │                        │
        │   peer registry · cross-host     │                        │
        └─────────────────┬────────────────┘                        │
                          │  reads status                           │
                          │  (one-way dep: orochi → sac)            │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   scitex-agent-container (sac)   │                        │
        │   lifecycle · health · restart   │                        │
        │   apptainer runtime · per host   │                        │
        │   (zero knowledge of orochi)     │                        │
        └─────────────────┬────────────────┘                        │
                          │  starts / supervises                    │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   Claude agents (one per host)   │ ── heartbeat-push ──▶ orochi
        │   session.jsonl · SDK            │ ── alerts ─────────────┘
        └──────────────────────────────────┘
```

## Responsibility split

| Concern                                                   | Owner                                      |
|-----------------------------------------------------------|--------------------------------------------|
| Agent process (SDK + session.jsonl)                       | **sac**                                    |
| Per-host control plane (start/stop/send/tail/list)        | **sac**                                    |
| Container runtime (apptainer)                             | **sac**                                    |
| Cross-host message routing                                | **orochi**                                 |
| Human chatops UI (Slack-like web interface)               | **orochi**                                 |
| In-session push (MCP channel server)                      | **orochi** ships MCP; sac runs the agent   |
| SSH mesh / tunnel layer (cloudflared + autossh)           | **orochi**                                 |
| Peer registry                                             | **orochi** (`~/.scitex/orochi/peers.yaml`) |

**Rule: sac knows containers + sessions on one host; orochi knows messages + people across hosts.**

## How orochi consumes sac

orochi reads `~/.scitex/agent-container/runtime/<name>/heartbeat.json` and
`session.jsonl` from each host it manages. It never calls `sac` CLI commands
directly — the contract is the on-disk state files.

Agents push heartbeats and alerts to orochi via the `server:orochi-push` MCP
channel, configured in `spec.claude.channels`:

```yaml
spec:
  claude:
    channels:
      - server:orochi-push
```

## Standalone use

sac works fully without orochi. If you don't need cross-host chatops, just
omit `server:orochi-push` from `spec.claude.channels` and skip the orochi
install entirely.

## Live instance

[https://scitex-orochi.com](https://scitex-orochi.com)
