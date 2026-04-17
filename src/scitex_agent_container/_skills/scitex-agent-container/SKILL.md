---
name: scitex-agent-container
description: Declarative YAML-based AI agent lifecycle management with tmux/screen, SSH remote deployment, modular auto-accept, live pane inspection, Claude Code hook ingestion, and a typed PaneAction engine.
---

# scitex-agent-container

Declarative lifecycle management for AI coding agents (Claude Code,
Cursor, Aider). Define an agent in YAML, launch it in a
tmux (default) or screen session locally or on a remote host via SSH,
and observe it through a rich non-agentic status surface.

## What the package ships

| Surface | Location |
|---|---|
| Python API | `scitex_agent_container` (`AgentConfig`, `load_config`, `validate_config`, `agent_start`, `agent_stop`, `agent_restart`, `agent_status`, `agent_logs`, `Registry`) |
| CLI entry points | `scitex-agent-container`, `sac` (see [cli.md](cli.md)) |
| MCP servers | None bundled — agents spawn their own via `src_mcp.json` |
| Runtimes | `runtimes/tmux.py`, `runtimes/screen.py`, `runtimes/claude_code.py`, `runtimes/apptainer.py`, `runtimes/docker.py`, `runtimes/sbatch_spartan.py`, `runtimes/ssh_remote.py` |
| PaneActions | `actions/nonce_probe.py`, `actions/compact.py` (typed, logged via `action_store` to `~/.scitex/agent-container/actions.db`) |
| Observability | `agent_meta.collect_rich()`, `event_log`, `snapshot`, `hooks.hook_event`, `liveness_probe`, `quota_watch` |
| Config format | v2 `scitex-agent-container/v2` (auto-derived fields); legacy v1 `cld-agent/v1` still supported |

## Quick Reference

| Topic | File |
|------|-------|
| v2 config format, auto-derived fields, `src_*` deployment | [config-v2.md](config-v2.md) |
| tmux vs screen, `capture_content`, `send_keys`, `send_text_and_submit` | [multiplexer.md](multiplexer.md) |
| Modular TUI prompt handlers, extending, diagnostics | [auto-accept.md](auto-accept.md) |
| SSH deployment, `src_*` copying, preflight, venv | [remote-deploy.md](remote-deploy.md) |
| Full CLI + Python API reference | [cli.md](cli.md) |
| Common launch failures and recovery | [troubleshooting.md](troubleshooting.md) |
| Resource heartbeat / cache contract (cross-package) | [resource-heartbeat.md](resource-heartbeat.md), [resource-management.md](resource-management.md) |

## 30-second start

```bash
pip install scitex-agent-container
scitex-agent-container start my-agent.yaml
scitex-agent-container status my-agent --json | jq .pane_state
scitex-agent-container attach my-agent           # Ctrl-B D to detach
```

## Observability contract

`status <name> --json` merges the registry entry with `agent_meta.collect_rich()`
and `event_log.summarize()`. Downstream orchestrators (e.g. scitex-orochi)
consume that JSON — no direct coupling. Every field is best-effort; failures
leave the default (`""`, `0`, `[]`) rather than raising. See the README's
"Rich Status" table for the full field list.

## PaneActions

Typed, logged vocabulary for pane-mediated operations. Each action subclasses
`PaneAction` and implements `snapshot` / `precheck` / `send` / `is_complete`.
`run_action` classifies every attempt (`success`, `precondition_fail`,
`send_error`, `completion_timeout`, `skipped_by_policy`) and writes the row to
the host-wide SQLite store at `~/.scitex/agent-container/actions.db`.
Built-ins: `NonceProbeAction` (functional liveness) and `CompactAction`
(context-window compaction with drop-verification). See
`scitex-agent-container actions --help`.
