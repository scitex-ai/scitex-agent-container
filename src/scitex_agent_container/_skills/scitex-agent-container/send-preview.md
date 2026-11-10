---
description: |
  [TOPIC] Send argument preview
  [DETAILS] Preview CLI, Python or MCP send arguments without resolving an
  endpoint, reading target credentials, starting an agent or sending a turn.
tags: [scitex-agent-container-send-preview]
---

# Preview send arguments

```bash
sac agents send worker "review this change" --dry-run
```

```python
from scitex_agent_container import agent

plan = agent.send("worker", prompt="review this change", dry_run=True)
```

MCP exposes the same optional `dry_run=True` on `agent_send`. Python/MCP
also accept a control `key` instead of `prompt`; exactly one must be supplied.
Preview takes precedence over `wait` and hides prompt/control-key contents,
reporting the input kind and character count. CLI preview reports `wait=True`
to describe its ordinary delivery attempt; a host can still return pending.

The JSON plan has `scope="arguments_only"` and `dry_run=true`. It does not
resolve the target, validate options against a provider, check credentials,
inspect agent state, start an agent, or establish delivery/completion. Shared
CLI startup may initialize its usual log file; preview is not a promise that
the entire process performs no filesystem I/O.

Ordinary Python/MCP `wait=False` is different: it may inspect state, probe a
local endpoint or attempt policy-controlled autostart, then return a delivery
plan. `status="dispatched"` alone is not evidence a prompt was delivered.
A validated host HTTP 202 is pending even with `wait=True`; poll the authored
exchange without resending. CLI write approval is a separate contract; this
preview does not change ordinary sends or authorize an operation.

The owning implementations are `cli_pkg/_send_preview.py`, `_send.py`,
`send_cmds.py` and `_mcp/_tools/_agent.py`. Behavioral controls are in
`tests/scitex_agent_container/cli_pkg/test__send_preview.py`; host receipt and
refusal controls remain in `test__send_host.py`.
