---
name: scitex-agent-container-env-vars
description: Environment variables read by scitex-agent-container at import / runtime. Follow SCITEX_<MODULE>_* convention — see general/10_arch-environment-variables.md.
tags: [scitex-agent-container, scitex-package]
---

# scitex-agent-container — Environment Variables

With ~40 `SCITEX_*` vars in source, this leaf groups them by purpose. For the
exact authoritative list, run the audit snippet at the bottom.

## Container identity / metadata

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_ID` | Stable container ID (auto-assigned on first boot). | auto | string |
| `SCITEX_AGENT_CONTAINER_NAME` | Human-readable container name. | auto | string |
| `SCITEX_AGENT_CONTAINER_HOSTNAME` | Override container hostname. | host | string |
| `SCITEX_AGENT_CONTAINER_ROLE` | Role in fleet (e.g. `worker`, `observer`). | `worker` | string |
| `SCITEX_AGENT_CONTAINER_META_VERSION` | Metadata schema version. | current | string |
| `SCITEX_AGENT_CONTAINER_AGENT` | Agent engine (`claude` / `codex` / ...). | `claude` | string |
| `SCITEX_AGENT_CONTAINER_AGENT_META_SCRIPT` | Path to agent-metadata generator script. | bundled | path |
| `SCITEX_AGENT_CONTAINER_MODEL` | LLM model ID injected into the agent. | `—` | string |
| `SCITEX_AGENT_CONTAINER_SCREEN_NAME` | GNU-screen session name attached to the agent. | auto | string |
| `SCITEX_AGENT_NAME` | Short agent identifier (used in telemetry). | auto | string |

## Paths

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_CONFIG_PATH` | Path to the YAML config. | bundled | path |
| `SCITEX_AGENT_CONTAINER_YAML_DIRS` | Extra dirs scanned for YAML overrides (colon-separated). | unset | string (paths) |
| `SCITEX_AGENT_CONTAINER_REGISTRY_DIR` | Directory where the container registers its presence. | `~/.scitex/agent-container/registry` | path |
| `SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR` | Directory for SLURM-job state handoff. | `~/.scitex/agent-container/slurm` | path |
| `SCITEX_AGENT_CACHE_DIR` | Agent-local cache directory. | `~/.cache/scitex-agent` | path |

## Credentials

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY` | Anthropic API key used only under CI. | `—` | string (required in CI) |
| `SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN` | Telegram bot token for agent bridge. | `—` | string |

## Context compaction

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_COMPACT_ENABLED` | Enable auto-compaction of context window. | `true` | bool |
| `SCITEX_AGENT_COMPACT_THRESHOLD_PCT` | Trigger compaction at this context % used. | `80` | int |
| `SCITEX_AGENT_COMPACT_MIN_DROP_PCT` | Minimum % that must drop per pass. | `20` | int |
| `SCITEX_AGENT_COMPACT_MIN_INTERVAL_S` | Minimum seconds between compactions. | `300` | int |
| `SCITEX_AGENT_COMPACT_TIMEOUT_S` | Timeout per compaction attempt. | `60` | int |

## Action / probe / heartbeat timing

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_ACTION_RETENTION_DAYS` | How many days to keep action logs. | `30` | int |
| `SCITEX_AGENT_ACTION_SNAPSHOT_MAX_CHARS` | Truncate action snapshots above this size. | `8192` | int |
| `SCITEX_AGENT_AUTO_RESPONSE_TICK_S` | Auto-response poll interval. | `5` | int |
| `SCITEX_AGENT_KEY_DELAY_S` | Delay between simulated keystrokes. | `0.05` | float |
| `SCITEX_AGENT_SUBMIT_SETTLE_S` | Seconds to wait after submitting a prompt. | `1.0` | float |
| `SCITEX_AGENT_PROBE_INTERVAL_S` | Liveness probe interval. | `30` | int |
| `SCITEX_AGENT_PROBE_POLL_INTERVAL_S` | Fine-grained poll step inside a probe. | `1` | int |
| `SCITEX_AGENT_PROBE_TIMEOUT_S` | Probe timeout. | `10` | int |
| `SCITEX_HEARTBEAT_INTERVAL` | Ecosystem-wide heartbeat interval (fleet-shared). | `60` | int |

## Hook context (read-only, set by the harness)

| Variable | Purpose |
|---|---|
| `SCITEX_HOOK` | Name of the hook currently running. |
| `SCITEX_HOOK_CTX_*` | Per-hook context keys (dynamic prefix). |

## Cross-package (orochi fleet integration)

scitex-agent-container also reads the `SCITEX_OROCHI_*` family when joining
the orochi fleet — see `scitex-orochi/21_convention-env-vars.md` for the
authoritative list. The keys used here: `SCITEX_OROCHI_AGENT`,
`SCITEX_OROCHI_CHANNELS`, `SCITEX_OROCHI_MACHINE`, `SCITEX_OROCHI_MODEL`,
`SCITEX_OROCHI_TOKEN`, `SCITEX_OROCHI_URL`.

## Feature flags

- **opt-out:** `SCITEX_AGENT_COMPACT_ENABLED=false` disables context compaction.
- No opt-in flags in this package.

## Audit

```bash
grep -rhoE 'SCITEX_[A-Z0-9_]+' $HOME/proj/scitex-agent-container/src/ | sort -u
```
