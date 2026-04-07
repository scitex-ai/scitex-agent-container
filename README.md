<!-- SciTeX Convention: Header (logo, tagline, badges) -->
# scitex-agent-container

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-blue-cropped.png" alt="SciTeX" width="400">
  </a>
</p>

<p align="center"><b>Declarative YAML-based AI agent lifecycle management</b></p>

<p align="center">
  <a href="https://badge.fury.io/py/scitex-agent-container"><img src="https://badge.fury.io/py/scitex-agent-container.svg" alt="PyPI version"></a>
  <a href="https://scitex-agent-container.readthedocs.io/"><img src="https://readthedocs.org/projects/scitex-agent-container/badge/?version=latest" alt="Documentation"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/ci.yml"><img src="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/ci.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
</p>

<p align="center">
  <code>pip install scitex-agent-container</code>
</p>

---

<!-- SciTeX Convention: Problem & Solution -->
## Problem

Managing AI coding agents (Claude Code, Cursor, Aider) in production requires manual script-writing, environment setup, and process monitoring for each agent instance. Scaling from one agent to a fleet across multiple machines means duplicating fragile shell scripts with no health checks, restart policies, remote deployment, or inter-agent communication.

## Solution

scitex-agent-container provides declarative YAML definitions that fully specify an agent -- runtime, model, channels, environment, health checks, remote host, Orochi hub connection -- started with a single command:

```
YAML manifest --> scitex-agent-container start --> screen session
                                                   + remote SSH deploy
                                                   + Orochi auto-connect
                                                   + health monitor
                                                   + restart policy
```

<!-- SciTeX Convention: Installation -->
## Installation

Requires Python >= 3.10.

```bash
pip install scitex-agent-container

# With Orochi hub integration
pip install scitex-agent-container[orochi]

# With Telegram integration
pip install scitex-agent-container[telegram]

# Development
pip install scitex-agent-container[dev]
```

<!-- SciTeX Convention: Quickstart -->
## Quickstart

1. Write a YAML manifest:

```yaml
apiVersion: cld-agent/v1
kind: Agent
metadata:
  name: my-agent
  labels:
    role: worker
    machine: local
spec:
  runtime: claude-code
  model: sonnet
  workdir: ~/proj

  claude:
    flags:
      - --dangerously-skip-permissions
    session: new

  # Auto-connect to Orochi hub
  orochi:
    enabled: true
    hosts:
      - 192.168.11.22      # LAN (fast)
      - scitex-orochi.com   # domain (fallback)
    port: 8559
    token_env: SCITEX_OROCHI_TOKEN
    channels:
      - "#general"

  health:
    enabled: true
    interval: 30
    method: screen-alive

  restart:
    policy: on-failure
    max_retries: 3
    backoff:
      initial: 30
      max: 300
      multiplier: 2
```

2. Start and monitor:

```bash
scitex-agent-container start agent.yaml
scitex-agent-container status my-agent
scitex-agent-container logs my-agent -n 100
scitex-agent-container attach my-agent   # Ctrl-A D to detach
```

## Remote SSH Deployment

Deploy agents to remote machines with a single YAML:

```yaml
spec:
  remote:
    host: spartan          # SSH hostname
    user: ywatanabe
    timeout: 120           # seconds (HPC module loads are slow)
    login_shell: true      # bash -l -c (needed for PATH on most hosts)
```

```bash
# Preflight checks (SSH, screen, python, disk) then start
scitex-agent-container start remote-agent.yaml

# Skip preflight for slow hosts (e.g. HPC with module loads)
scitex-agent-container start --no-preflight remote-agent.yaml

# Run preflight checks without starting
scitex-agent-container check remote-agent.yaml
```

## Orochi Auto-Connect

Agents auto-register with the [scitex-orochi](https://github.com/ywatanabe1989/scitex-orochi) WebSocket hub on startup:

```yaml
spec:
  orochi:
    enabled: true
    hosts:                      # tried in order, first reachable wins
      - 127.0.0.1              # localhost (if hub runs here)
      - 192.168.11.22          # LAN IP
      - scitex-orochi.com      # domain (external fallback)
    port: 8559
    token_env: SCITEX_OROCHI_TOKEN
    channels: ["#general", "#research"]
    heartbeat_interval: 60
    reconnect_interval: 10
    reconnect_max_retries: 0    # 0 = infinite
```

No silent fallbacks -- every host attempt is logged:
```
INFO  Orochi connection report: [192.168.11.22:FAIL | scitex-orochi.com:OK]
      -- connected via scitex-orochi.com (my-agent@spartan channels=['#general'])
```

<!-- SciTeX Convention: Four Interfaces -->
## Four Interfaces

<details>
<summary><strong>Python API</strong></summary>

<br>

```python
from scitex_agent_container import (
    AgentConfig, load_config, validate_config,
    agent_start, agent_stop, agent_restart, agent_status, agent_logs,
    Registry,
)

config = load_config("agent.yaml")      # Parse YAML manifest
agent_start("agent.yaml")               # Launch agent
info = agent_status("my-agent")         # Query status
agent_stop("my-agent")                  # Stop agent
agent_restart("my-agent")               # Restart agent
output = agent_logs("my-agent")         # Read logs
registry = Registry()                    # Access agent registry
```

</details>

<details>
<summary><strong>CLI Commands</strong></summary>

<br>

```bash
scitex-agent-container --help-recursive          # Show all commands

# Lifecycle
scitex-agent-container start <config.yaml>       # Start an agent
scitex-agent-container start --no-preflight ...  # Skip SSH preflight checks
scitex-agent-container stop <name>               # Stop an agent
scitex-agent-container restart <name>            # Restart an agent
scitex-agent-container attach <name>             # Attach to screen session

# Inspection
scitex-agent-container status [name] [--json]    # Show agent status
scitex-agent-container list [--json]             # List all agents
scitex-agent-container list --capability gpu     # Filter by capability
scitex-agent-container list --machine spartan    # Filter by machine
scitex-agent-container ps [--json]               # Alias for list
scitex-agent-container logs <name> [-n LINES]    # Show recent output
scitex-agent-container health <name> [--json]    # Run health check
scitex-agent-container find --capability gpu     # Find agents by label

# Configuration
scitex-agent-container validate <config.yaml>    # Validate YAML syntax
scitex-agent-container check <config.yaml>       # Run full preflight checks
scitex-agent-container build [--runtime docker|apptainer]

# Maintenance
scitex-agent-container cleanup                   # Remove stale entries
scitex-agent-container list-python-apis [-v]     # List public API tree
scitex-agent-container version                   # Show version
```

</details>

<details>
<summary><strong>MCP Server -- for AI Agents</strong></summary>

<br>

Not yet implemented. Planned for a future release.

</details>

<details>
<summary><strong>Skills -- for AI Agent Discovery</strong></summary>

<br>

Agent skills are declared in the YAML manifest and injected into the agent's CLAUDE.md at startup:

```yaml
spec:
  skills:
    required:
      - python-scitex     # Auto-loaded at startup
      - data-analysis
    available:
      - scitex             # Available but not auto-loaded
```

</details>

## YAML Spec Reference

| Section | Key Fields | Description |
|---------|-----------|-------------|
| `metadata` | `name`, `labels` | Agent identity and capability labels |
| `spec.runtime` | `claude-code`, `cursor`, `aider` | AI coding tool to use |
| `spec.model` | `sonnet`, `opus[1m]` | Model selection |
| `spec.remote` | `host`, `user`, `timeout`, `login_shell` | SSH remote deployment |
| `spec.orochi` | `hosts[]`, `port`, `token_env`, `channels[]` | Orochi hub auto-connect |
| `spec.claude` | `channels[]`, `flags[]`, `session` | Claude Code-specific options |
| `spec.health` | `enabled`, `interval`, `method` | Health monitoring |
| `spec.restart` | `policy`, `max_retries`, `backoff` | Auto-restart on failure |
| `spec.watchdog` | `enabled`, `interval`, `responses` | Auto-respond to prompts |
| `spec.skills` | `required[]`, `available[]` | Skill injection |
| `spec.container` | `runtime`, `image`, `volumes` | Docker/Apptainer container |
| `spec.telegram` | `bot_token_env`, `allowed_users` | Telegram integration |
| `spec.screen` | `name` | Screen session name override |
| `spec.env` | key-value pairs | Environment variables |
| `spec.hooks` | `pre_start`, `post_start`, `pre_stop`, `post_stop` | Lifecycle hooks |

<!-- SciTeX Convention: Ecosystem -->
## Part of SciTeX

scitex-agent-container is part of [**SciTeX**](https://scitex.ai). It depends on [scitex-container](https://github.com/ywatanabe1989/scitex-container) for container runtime abstractions and is used by [scitex-orochi](https://github.com/ywatanabe1989/scitex-orochi) for multi-machine agent orchestration.

<!-- SciTeX Convention: Footer (Four Freedoms + icon) -->
>Four Freedoms for Research
>
>0. The freedom to **run** your research anywhere -- your machine, your terms.
>1. The freedom to **study** how every step works -- from raw data to final manuscript.
>2. The freedom to **redistribute** your workflows, not just your papers.
>3. The freedom to **modify** any module and share improvements with the community.
>
>AGPL-3.0 -- because we believe research infrastructure deserves the same freedoms as the software it runs on.

---

<p align="center">
  <a href="https://scitex.ai" target="_blank"><img src="docs/scitex-icon-navy-inverted.png" alt="SciTeX" width="40"/></a>
</p>

<!-- EOF -->
