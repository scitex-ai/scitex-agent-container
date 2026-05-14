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
| Per-agent local inbox (`POST /agents/<name>/turn`) | **sac**                                    |
| In-session push (MCP channel server `server:sac`)         | **sac** — connects to local `sac listen` SSE stream and pushes `notifications/claude/channel` into the running session |
| Cross-host message routing                                | **orochi**                                 |
| Human chatops UI (Slack-like web interface)               | **orochi**                                 |
| SSH mesh / tunnel layer (cloudflared + autossh)           | **orochi**                                 |
| Peer registry                                             | **orochi** (`~/.scitex/orochi/peers.yaml`) |

**Rule: sac owns containers + sessions + the local channel primitive on one host; orochi owns messages + people + transport across hosts.** `server:orochi-push` remains orochi's own MCP channel (for orochi-mediated cross-host topics + chat); `server:sac` is sac's local primitive that any peer reaches by POSTing to the host's `sac listen` — whether the network in between is orochi-mediated, an operator's SSH tunnel, or nothing at all (same host).

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
