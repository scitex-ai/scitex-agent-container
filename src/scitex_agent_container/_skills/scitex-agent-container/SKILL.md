---
name: scitex-agent-container
description: Deploy and manage Claude Code agents via YAML config, with Orochi integration, SSH remote deployment, and zero-trust isolation.
---

# scitex-agent-container

Declarative agent deployment. Define agents in YAML, launch them in screen sessions with auto-connect to Orochi hub.

## Agent Config (YAML)

```yaml
apiVersion: cld-agent/v1
kind: Agent
metadata:
  name: my-agent
  labels:
    machine: mba
    role: researcher
spec:
  runtime: claude-code
  model: sonnet
  workdir: ~/proj
  orochi:
    enabled: true
    hosts:
      - 192.168.0.102
      - orochi.example.com
    port: 8559
    ws_path: /ws/agent/
    token_env: SCITEX_OROCHI_TOKEN
    channels:
      - "#general"
      - "#research"
```

## OrochiSpec Fields

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable Orochi auto-connect |
| `hosts` | `[]` | Host list, tried in order (first reachable wins) |
| `port` | `8559` | Django Channels port (HTTP + WS unified) |
| `ws_path` | `/ws/agent/` | WebSocket endpoint path |
| `token_env` | `SCITEX_OROCHI_TOKEN` | Env var holding auth token |
| `channels` | `[]` | Channels to subscribe |
| `heartbeat_interval` | `30` | Seconds between heartbeats |
| `reconnect_interval` | `10` | Seconds between reconnect attempts |
| `reconnect_max_retries` | `0` | 0 = infinite retries |

## MCP Config Auto-Generation (orochi_mcp.py)

When `orochi.enabled: true`, the launcher:
1. Locates `mcp_channel.ts` (env override, package path, or `/opt/`)
2. Builds MCP server config with agent name, host, port, channels
3. Writes to `~/.scitex/agent-container/cache/mcp-configs/mcp-<name>.json` (NOT workdir)
4. Adds `--mcp-config` and `--dangerously-load-development-channels` flags

Path isolation matters: workdir may be shared with Telegram or other sessions.

## Auto-Accept Watchdog

Handles TUI prompts that block unattended agents (skills trust, permissions, channel flags).

- Polling-based: inspects screen PTY at configurable interval
- Sends `\r` (Enter) to accept defaults, not `y\n`
- Configurable responses per prompt type in YAML:

```yaml
watchdog:
  enabled: true
  interval: 1.5
  responses:
    y_n: "1"
    y_y_n: "2"
    waiting: "/speak-and-call"
```

## Zero-Trust Guards

Four layers prevent cross-contamination:

1. **`SCITEX_OROCHI_DISABLE=true`** -- env var kill switch
2. **`CLAUDE_AGENT_ROLE=telegram`** -- role-based blocking
3. **`TELEGRAM_BOT_TOKEN` detection** -- context-based blocking
4. **MCP config to `/tmp/`** -- session isolation

Guards run at flag-generation time (before Claude Code launches). Truthy: `true`, `1`, `yes`, `enable`, `enabled`.

## Telegram Integration (Telegrammer Flow)

### Credential Cascade

```
ENV (SCITEX_OROCHI_TELEGRAM_BOT_TOKEN)
  ▼
scitex-orochi
  agents/orochi-telegrammer.yaml (bot_token_env references env var)
  ▼
scitex-agent-container  ◀── YOU ARE HERE
  1. Reads bot_token_env from YAML
  2. Resolves token from os.environ
  3. Exports into screen session
  4. Writes access.json
  5. Launches watchdog
  ▼
claude-code-telegrammer
  TUI watchdog, receives token via env
```

### Key Points

- `bot_token_env` in YAML → resolved from `os.environ` at runtime
- `access.json` written to `TELEGRAM_STATE_DIR` (`~/.scitex/agent-container/telegram/{bot_id}/`)
- Zero-trust guards prevent telegram agents from loading Orochi MCP
- `CLAUDE_AGENT_ROLE=telegram` + `SCITEX_OROCHI_DISABLE=true` set automatically
- MCP config isolation: telegrammer never sees Orochi channel config

## SSH Remote Deployment

Deploy agents to remote hosts via SSH:

```yaml
remote:
  host: 192.168.0.200
  user: ywatanabe
  key: ~/.ssh/id_ed25519
  port: 22
  login_shell: true
```

The launcher SSHs into the remote, creates a screen session, and launches Claude Code there. `login_shell: true` ensures PATH is set correctly.

## Multi-Host Fallback

Connection attempts produce a report for every host:

```
Orochi connection report: [192.168.0.102:OK] -- connected via 192.168.0.102
Orochi connection report: [192.168.0.102:FAIL | orochi.example.com:OK] -- connected via orochi.example.com
Orochi connection report: [192.168.0.102:FAIL | orochi.example.com:FAIL] -- ALL HOSTS FAILED (attempt 3)
```

No silent fallback. Every host result is logged.

## Message Format

Django Channels uses flat message format: `msg.text` (not `msg.payload.text`).
