# scitex-agent-container

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-blue-cropped.png" alt="SciTeX" width="400">
  </a>
</p>

<p align="center"><b>Declarative YAML-based AI agent lifecycle management with tmux/screen, SSH remote deploy, health checks, and auto-accept.</b></p>

<p align="center">
  <a href="https://scitex-agent-container.readthedocs.io/">Full Documentation</a> · <code>pip install scitex-agent-container</code>
</p>

<!-- scitex-badges:start -->
<p align="center">
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/v/scitex-agent-container.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/pyversions/scitex-agent-container.svg" alt="Python"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/test.yml"><img src="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container"><img src="https://codecov.io/gh/ywatanabe1989/scitex-agent-container/graph/badge.svg" alt="Coverage"></a>
  <a href="https://scitex-agent-container.readthedocs.io/en/latest/"><img src="https://readthedocs.org/projects/scitex-agent-container/badge/?version=latest" alt="Docs"></a>
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/license-AGPL_v3-blue.svg" alt="License: AGPL v3"></a>
</p>
<!-- scitex-badges:end -->

---

## Problem and Solution

| # | Problem | Solution |
|---|---------|----------|
| 1 | **Fragile per-agent scripts** — launching Claude Code means hand-rolling shell scripts for tmux, env vars, MCP configs, and auto-accept prompts, with no restart policy or health monitoring | **Declarative YAML manifest** — one file fully specifies runtime, model, MCP servers, env, health checks, and remote host; `sac start` brings the agent up in tmux/screen with auto-accept and a watchdog |
| 2 | **No fleet story** — scaling from one agent to many across machines duplicates the same fragile scripts, with no SSH deploy, no presence, and no inter-agent comms | **Remote deploy + state inspection** — `sac` copies src files, installs the venv over SSH, and keeps a live view of every pane's state so the fleet behaves as one unit |

## Problem

Managing AI coding agents (Claude Code) in production requires manual script-writing, environment setup, and process monitoring for each agent instance. Scaling from one agent to a fleet across multiple machines means duplicating fragile shell scripts with no health checks, restart policies, remote deployment, or inter-agent communication.

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

## Part of SciTeX

`scitex-agent-container` is part of [**SciTeX**](https://scitex.ai). Install via
the umbrella with `pip install scitex[agent-container]` to use as
`scitex.agent_container` (Python) or `scitex agent-container ...` (CLI).

## Templates

`config/templates/` ships six minimal pattern templates — copy and adapt:

| Template | Pattern | When to use |
|---|---|---|
| `local.yaml` | claude-code on local host | Default; shares operator's env (skills, MCP, venv) |
| `docker.yaml` | claude-code in Docker | Local isolation; `mount_host_claude` opt-in |
| `apptainer.yaml` | claude-code in Apptainer/Singularity | HPC compute nodes / locked-down hosts |
| `ssh.yaml` | claude-code via SSH on remote host | Cross-machine fleet member |
| `ssh-slurm.yaml` | SLURM-submitted job (with auto-resubmit) | Long-running compute on shared cluster |
| `mcp.yaml` | claude-code with MCP server wiring | Agent that needs MCP tool access |

Concrete real-world configs live in `config/examples/` (e.g. `newbie-docker.yaml`, `researcher-opus.yaml`). Both directories are validated by `tests/test_templates_v3_valid.py` — every shipped YAML must round-trip through `load_config`, and the SLURM template must additionally render a valid sbatch script.

To instantiate (dir-as-SSoT — agent name is derived from the parent directory):

```bash
mkdir -p ~/.scitex/orochi/agents/my-agent
cp config/templates/local.yaml ~/.scitex/orochi/agents/my-agent/my-agent.yaml
scitex-agent-container start my-agent
```

## 1 Interfaces

<details open>
<summary><strong>CLI</strong></summary>

<br>

```bash
sac start <agent-yaml>      # launch declared agent in tmux/screen with auto-accept + watchdog
sac stop <agent>            # graceful stop
sac show-status                  # live state of every pane
sac deploy <host>           # SSH-deploy fleet to remote host
sac --help-recursive        # full subcommand tree
```

</details>

## Quickstart (v2 config)

1. Create agent definition directory:

```
my-agent/
  my-agent.yaml     # Agent config
  src_CLAUDE.md      # -> deployed to {workdir}/CLAUDE.md
  src_mcp.json       # -> deployed to {workdir}/.mcp.json
  src_env            # -> deployed to {workdir}/.env  (mode 0600)
```

The `src_*` family is a generic file-deploy pipeline: a sibling file named `src_X` next to the YAML is materialized into the workspace at agent start, with `${VAR}` and `${metadata.name}` interpolation. `src_env` is the dotenv variant — sourceable by anything the agent spawns (cron jobs, ssh-launched commands, fresh shells), not just the multiplexer session. See [`_skills/scitex-agent-container/06_env-injection-ports.md`](src/scitex_agent_container/_skills/scitex-agent-container/06_env-injection-ports.md) for the four distinct env-injection ports and when to use each.

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
    method: multiplexer-alive

  restart:
    policy: on-failure
    max_retries: 3
```

v2 auto-derives from `metadata.name`: workdir, session name, env vars (CLAUDE_AGENT_ID, CLAUDE_AGENT_ROLE, etc.), and pre-start hooks. Sibling `src_CLAUDE.md` and `src_mcp.json` files are deployed to the workspace with `${metadata.name}` and `${ENV_VAR}` interpolation.

3. Start and monitor:

```bash
scitex-agent-container start my-agent.yaml
scitex-agent-container inspect my-agent         # Live state detection
scitex-agent-container show-status my-agent
scitex-agent-container show-logs my-agent -n 100
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

## SLURM (single-agent)

Submit an agent as an `sbatch` job that holds the allocation, runs claude in tmux on the compute node, and auto-resubmits before walltime via a `SIGUSR1` trap:

```yaml
spec:
  runtime: slurm
  slurm:
    partition: cascade
    cpus_per_task: 4
    mem: "16G"
    time_limit: "7-00:00:00"
    auto_resubmit: true
    hooks:
      pre_agent: ~/path/to/module-load.sh    # `module load Python/3.11.3` etc.
```

```bash
sac start head-spartan/head-spartan.yaml   # submits sbatch on the local SLURM submission host
sac attach head-spartan                    # srun --pty + tmux attach on the compute node
sac stop head-spartan                      # scancel + clear state
```

## SLURM (multi-tenant — many agents on one allocation)

Requires `pip install scitex-agent-container[slurm]` (pulls `scitex-hpc>=0.6.1`).

Book a reservation **once**, then launch many agents into the same allocation. Cuts queue wait from minutes per launch to one ssh round-trip per launch:

```bash
# Once: book a node for the day
scitex-hpc reservations book dev-pool \
    --host spartan --partition cascade \
    --cpus 8 --mem 32G --time 7-0 \
    --tmux-server sac --persistent

# All day: launch agents into it
sac start dev-helper.yaml         # tmux session in dev-pool's allocation
sac start doc-builder.yaml        # second tmux session, same allocation
sac start test-runner.yaml        # third, same allocation

sac attach dev-helper             # interactive on compute node

# When done with the day's pool:
scitex-hpc reservations release dev-pool
```

Tenant agent YAML — note the new runtime kind and the `slurm.reservation` field:

```yaml
spec:
  runtime: slurm-tenant
  slurm:
    reservation: dev-pool         # name of the existing scitex-hpc lease
  claude:
    flags: [--dangerously-skip-permissions]
```

The reservation's hold body bootstraps a long-lived tmux server as PID 1 of the sbatch script (via `--tmux-server sac`), so tenant tmux sessions survive past their setup commands. Without it, `srun --overlap` step cgroups would terminate them within seconds.

**Compatible with HPC policies banning persistent daemons** — every operation is bastion-initiated SSH, no `crontab @reboot`, no autossh, no tunnel. SLURM's documented `SIGUSR1` signal handles walltime auto-resubmit.

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
scitex-agent-container show-status [name] [--json]   # Rich status dict (see below)
scitex-agent-container list [--json] [--capability X] [--machine Y]
scitex-agent-container show-logs <name> [-n LINES]
scitex-agent-container check-health <name> [--json]
scitex-agent-container attach <name>

# Hook event ingestor (wired from Claude Code hooks, see below)
scitex-agent-container ingest-hook-event <pretool|posttool|prompt|stop|other>

# Pane actions (see "Pane Actions" below)
scitex-agent-container actions run <nonce-probe|compact> <agent> [--json]
scitex-agent-container actions query [--agent X] [--action Y] [--since 2h]
scitex-agent-container actions stats [--agent X] [--since 7d]
scitex-agent-container actions purge [--days N]

# A2A protocol — standalone agent endpoint, no fleet deps
# (echo handler by default; --handler claude_cli runs `claude --print`)
scitex-agent-container a2a serve <agent.yaml>... [--port 8888] [--handler echo|claude_cli|exec]

# Configuration
scitex-agent-container validate <config.yaml>
scitex-agent-container check <config.yaml>

# Maintenance
scitex-agent-container clean-registry
```

## Rich Status (`status <name> --json`)

`status <name> --json` returns a non-agentic snapshot of the agent suitable
for dashboards or fleet monitors. The payload merges the base registry
entry with fields from `agent_meta.collect_rich()` and
`event_log.summarize()`:

| Field | Description |
|---|---|
| `pane_text` | Recent tmux `capture-pane` output, secrets redacted |
| `pane_state` | Classified: `running` / `idle_prompt` / `y_n_prompt` / `auth_error` / `compose_pending_unsent` / `limit_reached` / `unknown` |
| `stuck_prompt_text` | Last line when `pane_state` indicates a blocking prompt |
| `claude_md` | Workspace `CLAUDE.md` contents (truncated) |
| `mcp_json` | Workspace `.mcp.json` with token-like values redacted |
| `recent_tools`, `recent_prompts` | Last N tool uses / user prompts from the hook ring-buffer |
| `agent_calls`, `background_tasks` | Subagent launches and `Bash run_in_background=true` starts |
| `tool_counts` | `{tool_name: count}` over the window |
| `last_tool_at`, `last_tool_name` | ISO timestamp and name of the newest `pretool` event (any tool) -- functional heartbeat, distinguishes "process alive" from "LLM actually producing tool calls" |
| `last_mcp_tool_at`, `last_mcp_tool_name` | Same, restricted to tools whose name starts with `mcp__` -- MCP sidecar health probe |
| `last_action_at`, `last_action_name` | ISO timestamp and name of the most recent `PaneAction` attempt. `last_action_name` (renamed from `last_action`) avoids a column collision with orochi's hub schema. |
| `last_action_outcome`, `last_action_elapsed_s` | Outcome (`success`, `precondition_fail`, `send_error`, `completion_timeout`, `skipped_by_policy`) and wall-clock duration of that attempt |
| `action_counts` | `{action_name: count}` rollup from `action_store.summarize()` |
| `p95_elapsed_s_by_action` | `{action_name: p95_seconds}` per-action latency headline |
| `context_pct`, `current_tool`, `current_task`, `last_user_msg`, `model_transcript` | Derived from the active Claude Code transcript JSONL |
| `quota_5h_used_pct`, `quota_7d_used_pct`, `quota_*_reset_at` | Claude usage (best-effort, cached) |
| `metrics` | Host-level CPU / memory / load / disk (psutil) |

Every field is best-effort: failures leave the default value (`""`,
`0`, `[]`) rather than raising.

```bash
scitex-agent-container show-status my-agent --json | jq '.pane_state, .recent_tools[-3:]'
```

## Claude Code Hook Integration

`hook-event` is the non-agentic counterpart to the status command: Claude
Code invokes it on every tool call / prompt / stop, and the handler
appends a compact JSON record to a per-agent ring-buffer at
`$XDG_DATA_HOME/.scitex/agent-container/events/<agent>.jsonl` (capped at
500 lines). `status --json` reads that buffer to populate
`recent_tools`, `recent_prompts`, `agent_calls`, `background_tasks`, and
`tool_counts`.

Wire it in the agent workspace's `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PreToolUse":       [{"matcher": "", "hooks": [
      {"type": "command", "command": "scitex-agent-container ingest-hook-event pretool"}
    ]}],
    "PostToolUse":      [{"matcher": "", "hooks": [
      {"type": "command", "command": "scitex-agent-container ingest-hook-event posttool"}
    ]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [
      {"type": "command", "command": "scitex-agent-container ingest-hook-event prompt"}
    ]}],
    "Stop":             [{"matcher": "", "hooks": [
      {"type": "command", "command": "scitex-agent-container ingest-hook-event stop"}
    ]}]
  }
}
```

Agent name resolution order: `--agent <name>` flag >
`SCITEX_OROCHI_AGENT` env var > `CLAUDE_AGENT_ID` env var > basename of
the current working directory. The handler swallows all errors so a
broken log can never block a tool call.

## Pane Actions

A typed, logged vocabulary for pane-mediated agent actions. Each
action is a `PaneAction` subclass implementing four methods
(`snapshot` / `precheck` / `send` / `is_complete`); the `run_action`
engine classifies every attempt as `success`, `precondition_fail`,
`send_error`, `completion_timeout`, or `skipped_by_policy`, and
writes it to a host-wide SQLite log at
`~/.scitex/agent-container/actions.db` (`agent` is a column, not a
path). Two concrete actions ship today:

- `NonceProbeAction` -- sends `Repeat <nonce>` and confirms the model
  echoes it back (true functional liveness, not just "process alive").
- `CompactAction` -- sends `/compact` and confirms by watching
  `context_pct` drop by at least `--min-drop-pct` (default 20).

```bash
# Run an attempt (non-zero exit on any non-SUCCESS / non-SKIPPED).
scitex-agent-container actions run nonce-probe <agent>
scitex-agent-container actions run compact <agent> \
    --min-drop-pct 30 --timeout 60 --json

# Query / aggregate / purge the attempt log.
scitex-agent-container actions query \
    --agent <agent> --action compact --since 2h --limit 20
scitex-agent-container actions stats --agent <agent> --since 7d
scitex-agent-container actions purge --days 14
```

The latest attempt is folded into `status --json` via
`agent_meta.collect_rich()` as `last_action_at` / `last_action_name` /
`last_action_outcome` / `last_action_elapsed_s`, with rollups
`action_counts` and `p95_elapsed_s_by_action`.

Reliable `send_keys` into a running pane needs an inter-key delay and
a settle window before `Enter`. Both are configurable via env vars
(read once at import time by `runtimes/tmux.py` and `runtimes/screen.py`):

| Env var | Default | Meaning |
|---|---|---|
| `SCITEX_AGENT_KEY_DELAY_S` | `0.1` | Delay between individual keys |
| `SCITEX_AGENT_SUBMIT_SETTLE_S` | `0.3` | Settle after text, before `Enter` |
| `SCITEX_AGENT_ACTION_RETENTION_DAYS` | `30` | Default `actions purge --days` horizon |

A `send_text_and_submit(session, text)` helper wraps the "type then
submit" pattern used by every action's `send`.

## Zero Coupling to Downstream Orchestrators

scitex-agent-container is a generic library. It knows nothing about
scitex-orochi, the hub, or any particular dashboard. `status --json`
emits a self-describing dict; downstream consumers (e.g. orochi's
`heartbeat-push` command) wrap it -- calling `status --json`, reshaping
the payload, and POSTing to whatever endpoint they own. Keeping the
two sides decoupled lets you swap the orchestrator, the transport, or
the schema without touching this package.

## YAML Spec Reference

| Section | Key Fields | Description |
|---------|-----------|-------------|
| `apiVersion` | `scitex-agent-container/v2`, `cld-agent/v1` | Config format version |
| `metadata` | `name`, `labels` | Agent identity and labels |
| `spec.runtime` | `claude-code`, `slurm`, `slurm-tenant` | Agent runtime selector |
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

>Four Freedoms for Research
>
>0. The freedom to **run** your research anywhere -- your machine, your terms.
>1. The freedom to **study** how every step works -- from raw data to final manuscript.
>2. The freedom to **redistribute** your workflows, not just your papers.
>3. The freedom to **modify** any module and share improvements with the community.
>
>AGPL-3.0 — because we believe research infrastructure deserves the same freedoms as the software it runs on.

---

<p align="center">
  <a href="https://scitex.ai" target="_blank"><img src="docs/scitex-icon-navy-inverted.png" alt="SciTeX" width="40"/></a>
</p>

<!-- EOF -->
