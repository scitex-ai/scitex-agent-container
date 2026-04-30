---
name: agent-container-cli
description: CLI commands and Python API for scitex-agent-container (and its short alias `sac`). Specialization of scitex-cli-convention.
tags: [scitex-agent-container, scitex-package]
---

> **This skill is a specialization of the canonical SciTeX CLI convention.**
> See: `scitex-python/src/scitex/_skills/general/interface-cli.md`
> (installed alongside the `scitex` package) for the canonical rules
> (noun-verb, universal flags, deprecation redirect, env vars,
> audit checklist). This file only lists scitex-agent-container-specific
> subcommands and the Python API.

# CLI

Two entry points are installed: `scitex-agent-container` and its
alias `sac`. Any subcommand accepts `--json` at the top level to emit
structured JSON.

```bash
scitex-agent-container [--json] <command> [ARGS]
scitex-agent-container --help-recursive     # Full command tree
scitex-agent-container --version
```

## Lifecycle

```bash
scitex-agent-container start <config.yaml|--all>
scitex-agent-container stop <name|yaml|--all>
scitex-agent-container restart <name|yaml>
scitex-agent-container cleanup              # Drop stale registry entries
```

## Inspection

```bash
scitex-agent-container status [name] [--json]      # Rich non-agentic snapshot
scitex-agent-container inspect <name> [--json]     # Live pane-state classifier
scitex-agent-container list [--json] [--capability X] [--machine Y]
scitex-agent-container health <name> [--json]
scitex-agent-container logs <name> [-n LINES]
scitex-agent-container attach <name>               # Interactive; Ctrl-B D detaches (tmux)
scitex-agent-container snapshot <name>             # Self-snapshot JSON
scitex-agent-container find --capability <label>   # Find agents by capability label
scitex-agent-container list-python-apis            # Enumerate public Python APIs
```

## Validation / preflight

```bash
scitex-agent-container validate <config.yaml>
scitex-agent-container check <name|yaml>           # Preflight (local and remote)
scitex-agent-container build                       # Build container base image
```

## Claude Code hook ingestor

```bash
scitex-agent-container hook-event <pretool|posttool|prompt|stop|other>
```

Invoked from Claude Code hooks (`PreToolUse`, `PostToolUse`,
`UserPromptSubmit`, `Stop`). Appends a compact JSON record to the
per-agent ring buffer at
`$XDG_DATA_HOME/.scitex/agent-container/events/<agent>.jsonl` (500-line
cap). `status --json` reads the buffer to populate `recent_tools`,
`recent_prompts`, `agent_calls`, `background_tasks`, `tool_counts`,
`last_tool_at/name`, and `last_mcp_tool_at/name`.

Agent name resolution: `--agent <name>` > `SCITEX_OROCHI_AGENT` env
var > `CLAUDE_AGENT_ID` env var > basename of CWD. All errors are
swallowed so a broken log cannot block a tool call.

## Pane actions (typed, logged)

```bash
scitex-agent-container actions run nonce-probe <agent> [--json]
scitex-agent-container actions run compact <agent> \
    [--min-drop-pct 20] [--timeout 60] [--json]
scitex-agent-container actions query [--agent X] [--action Y] \
    [--since 2h] [--limit 20]
scitex-agent-container actions stats [--agent X] [--since 7d]
scitex-agent-container actions purge [--days 30]
```

Attempts are persisted to `~/.scitex/agent-container/actions.db`
(SQLite). Latest attempt is folded into `status --json` as
`last_action_at`, `last_action_name`, `last_action_outcome`,
`last_action_elapsed_s`, plus `action_counts` and
`p95_elapsed_s_by_action` rollups.

## Account / quota management

```bash
scitex-agent-container account save <name>
scitex-agent-container account list
scitex-agent-container account switch <name>
scitex-agent-container account delete <name>

scitex-agent-container quota-watch                 # Auto-rotate on quota hit
```

## Environment knobs

| Env var | Default | Meaning |
|---|---|---|
| `SCITEX_AGENT_KEY_DELAY_S` | `0.1` | Delay between individual keys in `send_keys` |
| `SCITEX_AGENT_SUBMIT_SETTLE_S` | `0.3` | Settle window after text, before `Enter` |
| `SCITEX_AGENT_ACTION_RETENTION_DAYS` | `30` | Default horizon for `actions purge` |
| `SCITEX_OROCHI_AGENT` | — | Agent name used by `hook-event` when `--agent` is absent |
| `CLAUDE_AGENT_ID` | — | Fallback agent name for `hook-event` |
| `XDG_DATA_HOME` | `~/.local/share` | Base path for the event ring buffer |

# Python API

```python
from scitex_agent_container import (
    __version__,
    AgentConfig, load_config, validate_config,
    agent_start, agent_stop, agent_restart, agent_status, agent_logs,
    Registry,
)

# Multiplexer abstraction (tmux or screen).
from scitex_agent_container.runtimes.multiplexer import get_multiplexer
from scitex_agent_container.runtimes.prompts import (
    PROMPT_HANDLERS, PromptHandler, register_prompt,
)

# Rich observability surfaces used by `status --json`.
from scitex_agent_container.agent_meta import collect_rich
from scitex_agent_container.event_log import summarize as summarize_events
from scitex_agent_container.snapshot import take_snapshot, gather_snapshot, read_latest

# Typed pane actions.
from scitex_agent_container.action_base import PaneAction, run_action, ActionOutcome
from scitex_agent_container.actions.nonce_probe import NonceProbeAction
from scitex_agent_container.actions.compact import CompactAction
from scitex_agent_container.action_store import append_attempt, query, stats, summarize

config = load_config("agent.yaml")
mux = get_multiplexer(config)
content = mux.capture_content("name")
mux.send_keys("name", "2", "Enter")
```

Use `scitex-agent-container list-python-apis` to enumerate the public
surface programmatically — the authoritative list is generated from
the package, not the docs.
