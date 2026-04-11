---
name: scitex-agent-container
description: Declarative YAML-based AI agent lifecycle management with tmux/screen, SSH remote deployment, modular auto-accept, and live state inspection.
---

# scitex-agent-container

Declarative agent deployment. Define agents in YAML, launch them in tmux/screen sessions locally or on remote hosts via SSH.

## Config Versions

### v2 (recommended): `apiVersion: scitex-agent-container/v2`

Auto-derives workdir, session name, env vars from `metadata.name`. Uses sibling `src_CLAUDE.md` and `src_mcp.json` files.

```yaml
apiVersion: scitex-agent-container/v2
kind: Agent
metadata:
  name: my-agent
  labels:
    role: worker
    machine: local
spec:
  runtime: claude-code
  model: opus[1m]
  multiplexer: tmux  # tmux (default) or screen

  claude:
    flags:
      - --dangerously-skip-permissions
    session: new

  skills:
    required:
      - scitex
```

### v1 (legacy): `apiVersion: cld-agent/v1`

All fields explicit. Still fully supported.

## Agent Definition Directory

```
agents/my-agent/
  my-agent.yaml     # Agent config
  src_CLAUDE.md      # Section-injected into {workdir}/CLAUDE.md at start
  src_mcp.json       # Interpolated and copied to {workdir}/.mcp.json at start
```

`src_mcp.json` supports interpolation:
- `${metadata.name}` -> agent name from YAML
- `${ENV_VAR}` -> resolved from os.environ at deploy time
- `~/` in args -> expanded to full home path

## CLI

```bash
scitex-agent-container start <yaml|name>     # Start agent
scitex-agent-container stop <yaml|name>      # Stop agent
scitex-agent-container restart <yaml|name>   # Restart
scitex-agent-container inspect <name>        # Live pane state detection
scitex-agent-container status [name]         # Registry status
scitex-agent-container list [--json]         # List all agents
scitex-agent-container attach <name>         # Attach to session
scitex-agent-container logs <name>           # Captured output
scitex-agent-container health <name>         # Health check
scitex-agent-container cleanup               # Remove stale entries
```

## Multiplexer: tmux (default) vs screen

| Feature | tmux | screen |
|---------|------|--------|
| `capture-pane` | Works on macOS | Fails on macOS (hardcopy) |
| Auto-accept | Reliable | Unreliable on macOS |
| Socket dir | Consistent | Varies (SSH vs local) |
| Default | Yes | Legacy |

Set in YAML: `spec.multiplexer: tmux` or `spec.multiplexer: screen`

## Auto-Accept TUI Prompts

Modular prompt handlers in `runtimes/prompts.py`. Each handler detects a specific Claude Code TUI prompt and sends keystrokes:

```python
PromptHandler(
    name="bypass-permissions",
    detect=lambda c: "2. Yes, I accept" in c and "Bypass Permissions" in c,
    keys=["2", "Enter"],   # Send number key, not arrow keys
    priority=1,
)
```

Built-in handlers: bypass-permissions, dev-channels, thinking-effort, skip-permissions-yn.

Add new handlers:
```python
from scitex_agent_container.runtimes.prompts import register_prompt, PromptHandler
register_prompt(PromptHandler(
    name="my-new-prompt",
    detect=lambda c: "3. My Option" in c and "Enter to confirm" in c,
    keys=["3", "Enter"],
))
```

Diagnostics: `~/.scitex/agent-container/logs/{name}/auto-accept.log`

## Remote SSH Deployment

```yaml
spec:
  remote:
    host: mba
    user: ywatanabe
    timeout: 180
  venv: ~/.venv     # Activated on remote before commands
```

The launcher:
1. Copies YAML + `src_CLAUDE.md` + `src_mcp.json` to remote `/tmp/`
2. SSHs to remote and runs `scitex-agent-container start` there
3. Remote side handles auto-accept and startup commands

## Python API

```python
from scitex_agent_container import (
    AgentConfig, load_config, validate_config,
    agent_start, agent_stop, agent_restart, agent_status,
)
from scitex_agent_container.runtimes.multiplexer import get_multiplexer
from scitex_agent_container.runtimes.prompts import PROMPT_HANDLERS, register_prompt

config = load_config("agent.yaml")
mux = get_multiplexer(config)          # TmuxManager or ScreenManager
content = mux.capture_content("name")  # Read pane
mux.send_keys("name", "2", "Enter")   # Send keystrokes
```

## v2 Auto-Derived Fields

From `metadata.name` and `metadata.labels`:

| Field | Derived as |
|-------|-----------|
| `workdir` | `~/.scitex/orochi/workspaces/{name}` |
| `screen_name` | `{name}` |
| `env.CLAUDE_AGENT_ID` | `{name}` |
| `env.CLAUDE_AGENT_ROLE` | `{labels.role}` |
| `env.SCITEX_OROCHI_AGENT` | `{name}` |
| `env.SCITEX_OROCHI_MODEL` | human-readable from `spec.model` |
| `hooks.pre_start` | `mkdir -p {workdir}/.claude` |

All overridable by explicit values in the YAML.
