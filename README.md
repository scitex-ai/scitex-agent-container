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

## Problem

Managing AI coding agents (Claude Code, Cursor, Aider) in production requires manual script-writing, environment setup, and process monitoring for each agent instance. Scaling from one agent to a fleet across multiple machines means duplicating fragile shell scripts with no health checks, restart policies, remote deployment, or inter-agent communication.

## Solution

scitex-agent-container provides declarative YAML definitions that fully specify an agent -- runtime, model, MCP servers, environment, health checks, remote host -- started with a single command:

```
YAML manifest + src_CLAUDE.md + src_mcp.json
          |
          v
scitex-agent-container start
          |
          v
tmux/screen session + auto-accept TUI prompts
                     + remote SSH deploy
                     + health monitor
                     + restart policy
```

## Installation

Requires Python >= 3.10.

```bash
pip install scitex-agent-container
```

## Quickstart (v2 config)

1. Create agent definition directory:

```
my-agent/
  my-agent.yaml     # Agent config
  src_CLAUDE.md      # -> deployed to {workdir}/CLAUDE.md
  src_mcp.json       # -> deployed to {workdir}/.mcp.json
```

2. Write a YAML manifest:

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
  model: sonnet
  multiplexer: tmux       # tmux (default) or screen

  claude:
    flags:
      - --dangerously-skip-permissions
    # session: continue-or-new (default) | continue | new
    # continue-or-new: pass --continue iff a prior session exists for the
    #   workdir, else launch fresh. Preserves /compact history across
    #   rolling restarts without risking a hard failure.
    # continue: always pass --continue (fails if no prior session)
    # new:      never pass --continue
    session: continue-or-new

  skills:
    required:
      - scitex

  health:
    enabled: true
    interval: 60
    method: screen-alive

  restart:
    policy: on-failure
    max_retries: 3
```

v2 auto-derives from `metadata.name`: workdir, session name, env vars (CLAUDE_AGENT_ID, CLAUDE_AGENT_ROLE, etc.), and pre-start hooks. Sibling `src_CLAUDE.md` and `src_mcp.json` files are deployed to the workspace with `${metadata.name}` and `${ENV_VAR}` interpolation.

3. Start and monitor:

```bash
scitex-agent-container start my-agent.yaml
scitex-agent-container inspect my-agent         # Live state detection
scitex-agent-container status my-agent
scitex-agent-container logs my-agent -n 100
scitex-agent-container attach my-agent          # Ctrl-B D to detach (tmux)
```

## Remote SSH Deployment

Deploy agents to remote machines:

```yaml
spec:
  remote:
    host: mba              # SSH hostname
    user: ywatanabe
    timeout: 180
```

```bash
scitex-agent-container start remote-agent.yaml   # SSHs to remote, launches there
scitex-agent-container stop remote-agent.yaml     # Accepts name or YAML path
scitex-agent-container inspect my-remote-agent    # Live state from remote
```

## MCP Servers (src_mcp.json)

MCP config lives alongside the YAML as `src_mcp.json` -- visible, editable, version-controlled:

```json
{
  "mcpServers": {
    "scitex-orochi": {
      "type": "stdio",
      "command": "bun",
      "args": ["run", "~/proj/scitex-orochi/ts/mcp_channel.ts"],
      "env": {
        "SCITEX_OROCHI_URL": "wss://scitex-orochi.com",
        "SCITEX_OROCHI_AGENT": "${metadata.name}",
        "SCITEX_OROCHI_TOKEN": "${SCITEX_OROCHI_TOKEN}"
      }
    }
  }
}
```

`~` in args is expanded at deploy time. `${metadata.name}` interpolates from YAML. `${ENV_VAR}` resolves from the environment.

## Auto-Accept TUI Prompts

Claude Code shows confirmation prompts for dangerous flags. The auto-accept system handles them automatically using modular prompt handlers (`runtimes/prompts.py`):

```python
# Each handler: detect prompt text -> send number key + Enter
PromptHandler(name="bypass-permissions",
              detect=lambda c: "2. Yes, I accept" in c,
              keys=["2", "Enter"])
```

Handlers are order-agnostic, use numbered option text for reliability, and work with both tmux and screen. New prompts are added by appending to `PROMPT_HANDLERS`.

Diagnostics logged to `~/.scitex/agent-container/logs/{name}/auto-accept.log`.

## CLI Commands

```bash
# Lifecycle (accepts name or YAML path)
scitex-agent-container start <config.yaml>
scitex-agent-container stop <name|yaml>
scitex-agent-container restart <name|yaml>

# Inspection
scitex-agent-container inspect <name> [--json]   # Live pane state detection
scitex-agent-container status [name] [--json]
scitex-agent-container list [--json] [--capability X] [--machine Y]
scitex-agent-container logs <name> [-n LINES]
scitex-agent-container health <name> [--json]
scitex-agent-container attach <name>

# Configuration
scitex-agent-container validate <config.yaml>
scitex-agent-container check <config.yaml>

# Maintenance
scitex-agent-container cleanup
```

## YAML Spec Reference

| Section | Key Fields | Description |
|---------|-----------|-------------|
| `apiVersion` | `scitex-agent-container/v2`, `cld-agent/v1` | Config format version |
| `metadata` | `name`, `labels` | Agent identity and labels |
| `spec.runtime` | `claude-code`, `cursor`, `aider` | AI coding tool |
| `spec.model` | `sonnet`, `opus[1m]` | Model selection |
| `spec.multiplexer` | `tmux` (default), `screen` | Terminal multiplexer |
| `spec.remote` | `host`, `user`, `timeout` | SSH remote deployment |
| `spec.claude` | `flags[]`, `session`, `auto_accept` | Claude Code options. `session` values: `continue-or-new` (default, try `--continue` with graceful fallback), `continue` (strict resume), `new` (always fresh). Top-level `spec.session:` also accepted and takes precedence. |
| `spec.health` | `enabled`, `interval`, `method` | Health monitoring |
| `spec.restart` | `policy`, `max_retries`, `backoff` | Auto-restart |
| `spec.skills` | `required[]`, `available[]` | Skill injection |
| `spec.env` | key-value pairs | Environment variables |
| `spec.venv` | path | Python virtualenv to activate |
| `spec.hooks` | `pre_start`, `post_start`, `pre_stop`, `post_stop` | Lifecycle hooks |
| `spec.container` | `runtime`, `image`, `volumes` | Docker/Apptainer |

## Part of SciTeX

scitex-agent-container is part of [**SciTeX**](https://scitex.ai), used as a generic agent lifecycle library by downstream orchestrators like [scitex-orochi](https://github.com/ywatanabe1989/scitex-orochi) for multi-machine fleet dispatch.

>Four Freedoms for Research
>
>0. The freedom to **run** your research anywhere -- your machine, your terms.
>1. The freedom to **study** how every step works -- from raw data to final manuscript.
>2. The freedom to **redistribute** your workflows, not just your papers.
>3. The freedom to **modify** any module and share improvements with the community.
>
>AGPL-3.0

---

<p align="center">
  <a href="https://scitex.ai" target="_blank"><img src="docs/scitex-icon-navy-inverted.png" alt="SciTeX" width="40"/></a>
</p>

<!-- EOF -->
