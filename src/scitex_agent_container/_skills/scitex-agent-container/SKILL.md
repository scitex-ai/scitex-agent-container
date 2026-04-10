---
name: scitex-agent-container
description: Deploy and manage Claude Code agents via YAML config, with SSH remote deployment and an extensible plugin architecture.
---

# scitex-agent-container

Declarative agent deployment. Define agents in YAML, launch them in screen sessions locally or on remote hosts via SSH. Downstream packages (e.g. [scitex-orochi](https://github.com/ywatanabe1989/scitex-orochi)) consume this library to layer on MCP bridges, multi-machine dispatch, and hub registration without touching the core.

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
  claude:
    channels:
      - "#general"
      - "#research"
    flags:
      - --dangerously-skip-permissions
    session: new
```

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
2. **`SCITEX_AGENT_CONTAINER_ROLE=telegram`** -- role-based blocking
3. **`SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN` detection** -- context-based blocking
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
- `access.json` written to `SCITEX_AGENT_CONTAINER_TELEGRAM_STATE_DIR` (`~/.scitex/agent-container/telegram/{bot_id}/`)

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

## Message Format

Django Channels uses flat message format: `msg.text` (not `msg.payload.text`).
