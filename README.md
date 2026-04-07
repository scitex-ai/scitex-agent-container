# scitex-agent-container

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-banner.png" alt="SciTeX Agent Container" width="400">
  </a>
</p>

<p align="center"><b>Declarative YAML-based framework for defining, managing, and orchestrating AI coding agent instances</b></p>

<p align="center">
  <a href="https://badge.fury.io/py/scitex-agent-container"><img src="https://badge.fury.io/py/scitex-agent-container.svg" alt="PyPI version"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/ci.yml"><img src="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/ci.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
</p>

<p align="center">
  <code>pip install scitex-agent-container</code>
</p>

---

## Problem

Running AI coding agents (Claude Code, Cursor, Aider) in production requires manual screen session management, ad-hoc watchdog scripts, and no standard way to declare agent configurations. Scaling from one agent to a fleet means duplicating fragile shell scripts with no health checks, restart policies, or lifecycle management.

## Solution

scitex-agent-container lets you define agents as declarative YAML manifests and manage their full lifecycle through a single CLI:

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

2. Start the agent:

```bash
scitex-agent-container start config/examples/telegram-master.yaml
```

3. Monitor:

```bash
scitex-agent-container status telegram-master
scitex-agent-container logs telegram-master -n 100
scitex-agent-container attach telegram-master   # Ctrl-A D to detach
```

## CLI Reference

```
scitex-agent-container <command> [OPTIONS]

Lifecycle:
  start <config.yaml>       Start an agent from a YAML definition
  stop <name>               Stop a running agent
  restart <name>            Restart an agent
  attach <name>             Attach to an agent's screen session

Inspection:
  status [name] [--json]    Show agent status (one or all)
  list [--json]             List all registered agents
  ps [--json]               Alias for list
  logs <name> [-n LINES]    Show recent agent output
  health <name> [--json]    Run a health check on an agent

Configuration:
  validate <config.yaml>    Validate a YAML config file
  build [--runtime docker|apptainer] [--image TAG]
                            Build container base image

Maintenance:
  cleanup                   Remove stale registry entries
  list-python-apis [-v]     List public Python API tree

Global:
  --version                 Show version
  --help-recursive          Show help for all commands
```

## YAML Schema

Every manifest follows this structure:

| Section | Required | Description |
|---------|----------|-------------|
| `apiVersion` | yes | Schema version (`cld-agent/v1`) |
| `kind` | yes | Resource type (`Agent`) |
| `metadata.name` | yes | Unique agent name |
| `metadata.labels` | no | Key-value labels for filtering |
| `spec.runtime` | yes | `claude-code`, `cursor`, `aider` |
| `spec.model` | no | Model identifier (e.g., `opus[1m]`) |
| `spec.workdir` | no | Working directory for the agent |
| `spec.container` | no | Container config (`runtime`, `image`, `volumes`, `network`) |
| `spec.claude` | no | Claude Code specific (`channels`, `flags`, `session`) |
| `spec.env` | no | Environment variables |
| `spec.screen` | no | Screen session config (`name`) |
| `spec.telegram` | no | Telegram bot config (`bot_token_env`, `allowed_users`) |
| `spec.startup_commands` | no | Commands to send after launch |
| `spec.watchdog` | no | Auto-responder config (`enabled`, `interval`, `responses`) |
| `spec.health` | no | Health check config (`enabled`, `interval`, `method`) |
| `spec.restart` | no | Restart policy (`policy`, `max_retries`, `backoff`) |
| `spec.hooks` | no | Lifecycle hooks (`pre_start`, `post_start`, `pre_stop`, `post_stop`) |

## Architecture

```
scitex-agent-container start manifest.yaml
        |
        v
  +-- Config Loader --+
  |   Parse YAML      |
  |   Validate schema |
  +--------+----------+
           |
           v
  +-- Lifecycle Manager ------+
  |   1. Run pre_start hooks  |
  |   2. Launch runtime       |
  |   3. Start watchdog       |
  |   4. Start health monitor |
  |   5. Run post_start hooks |
  +--------+------------------+
           |
    +------+------+--------+
    |             |        |
    v             v        v
  Screen       Docker   Apptainer
  Session      Container Container
    |
    +-- Watchdog (poll screen, auto-respond)
    +-- Health Monitor (screen-alive check)
    +-- Restart Controller (backoff retry)
```

The **Registry** (`~/.scitex/agents/`) tracks all running agents as JSON entries, enabling `status`, `list`, and `cleanup` operations across sessions.

## Supported Runtimes

| Runtime | Status | Description |
|---------|--------|-------------|
| `claude-code` | Stable | Claude Code CLI in a screen session |
| `cursor` | Stub | Cursor editor (planned) |
| `aider` | Stub | Aider CLI (planned) |

## Container Support

| Container Runtime | Status | Use Case |
|-------------------|--------|----------|
| `screen` | Default | Bare metal, no isolation |
| `docker` | Supported | Local development, CI |
| `apptainer` | Supported | HPC clusters (rootless) |

Build a container image:

```bash
scitex-agent-container build --runtime docker --image scitex-agent:latest
scitex-agent-container build --runtime apptainer
```

## Part of SciTeX

scitex-agent-container is part of [**SciTeX**](https://scitex.ai).

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
