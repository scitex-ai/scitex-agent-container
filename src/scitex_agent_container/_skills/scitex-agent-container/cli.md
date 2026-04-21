---
name: agent-container-cli
description: CLI commands and Python API for scitex-agent-container (and its short alias `sac`). Specialization of scitex-cli-convention.
---

> **This skill is a specialization of `scitex-cli-convention`.**
> See: `~/.dotfiles/src/.scitex/orochi/shared/skills/scitex-cli-convention/SKILL.md`
> for the canonical rules (noun-verb, universal flags, `(Available Now)`,
> deprecation redirect, env vars, NDJSON, audit checklist). This file only
> lists scitex-agent-container-specific subcommands and the Python API.

# CLI Commands

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

# Python API

```python
from scitex_agent_container import (
    AgentConfig, load_config, validate_config,
    agent_start, agent_stop, agent_restart, agent_status,
)
from scitex_agent_container.runtimes.multiplexer import get_multiplexer
from scitex_agent_container.runtimes.prompts import PROMPT_HANDLERS, register_prompt

config = load_config("agent.yaml")
mux = get_multiplexer(config)
content = mux.capture_content("name")
mux.send_keys("name", "2", "Enter")
```
