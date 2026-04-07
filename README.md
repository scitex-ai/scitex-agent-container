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

## Problem

Managing AI coding agents (Claude Code, Cursor, Aider) in production requires manual script-writing, environment setup, and process monitoring for each agent instance. Scaling from one agent to a fleet means duplicating fragile shell scripts with no health checks, restart policies, or lifecycle management.

## Solution

scitex-agent-container provides declarative YAML definitions that fully specify an agent -- runtime, model, channels, environment, health checks -- started with a single command:

- **YAML-first configuration** -- one file defines runtime, model, watchdog, health checks, restart policy, and hooks
- **Pluggable runtimes** -- run agents bare on screen, inside Docker, or inside Apptainer (HPC clusters)
- **Built-in watchdog** -- auto-responds to Claude Code's permission prompts so agents run unattended
- **Health checks and restart policies** -- agents self-heal with configurable backoff

```
YAML manifest --> scitex-agent-container start --> screen session
                                                   + watchdog
                                                   + health monitor
                                                   + restart policy
```

## Installation

Requires Python >= 3.10.

```bash
pip install scitex-agent-container

# With Telegram integration
pip install scitex-agent-container[telegram]

# Development
pip install scitex-agent-container[dev]
```

## Quickstart

1. Write a YAML manifest:

```yaml
apiVersion: cld-agent/v1
kind: Agent
metadata:
  name: telegram-master
  labels:
    role: telegram
    team: core
spec:
  runtime: claude-code
  model: opus[1m]
  workdir: ~/proj

  claude:
    channels:
      - plugin:telegram@claude-plugins-official
    flags:
      - --dangerously-skip-permissions
    session: continue

  env:
    CLAUDE_AGENT_ROLE: telegram
    CLAUDE_AGENT_ID: telegram-master

  screen:
    name: cld-telegram

  watchdog:
    enabled: true
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: "/speak-and-call"

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
scitex-agent-container start config/examples/telegram-master.yaml
scitex-agent-container status telegram-master
scitex-agent-container logs telegram-master -n 100
scitex-agent-container attach telegram-master   # Ctrl-A D to detach
```

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
info = agent_status("telegram-master")   # Query status
agent_stop("telegram-master")            # Stop agent
agent_restart("telegram-master")         # Restart agent
output = agent_logs("telegram-master")   # Read logs
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
scitex-agent-container stop <name>               # Stop an agent
scitex-agent-container restart <name>            # Restart an agent
scitex-agent-container attach <name>             # Attach to screen session

# Inspection
scitex-agent-container status [name] [--json]    # Show agent status
scitex-agent-container list [--json]             # List all agents
scitex-agent-container ps [--json]               # Alias for list
scitex-agent-container logs <name> [-n LINES]    # Show recent output
scitex-agent-container health <name> [--json]    # Run health check

# Configuration
scitex-agent-container validate <config.yaml>    # Validate YAML config
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

Skills are planned for a future release. They will be available under `_skills/` and via the CLI.

</details>

## Part of SciTeX

scitex-agent-container is part of [**SciTeX**](https://scitex.ai). It depends on [scitex-container](https://github.com/ywatanabe1989/scitex-container) for container runtime abstractions and is used by [scitex-orochi](https://github.com/ywatanabe1989/scitex-orochi) for multi-machine agent orchestration.

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
