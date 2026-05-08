---
description: |
  [TOPIC] scitex-agent-container — Environment Variables
  [DETAILS] Environment variables read by scitex-agent-container at import / runtime. Follow SCITEX_<MODULE>_* convention — see general/10_arch-environment-variables.md..
tags: [scitex-agent-container-env-vars]
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

Canonical agent YAML location (fleet shared via dotfiles):
`~/.scitex/orochi/shared/agents/<name>/<name>.yaml` — the dir-as-SSoT
resolver walks per-host `<host>/agents/`, then `shared/agents/`, then
`agents/`. See `01_config-v3.md` for the full search path.

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_CONFIG_PATH` | Path to the YAML config. | bundled | path |
| `SCITEX_AGENT_CONTAINER_YAML_DIRS` | Extra dirs scanned for YAML overrides (colon-separated). | unset | string (paths) |
| `SCITEX_AGENT_CONTAINER_REGISTRY_DIR` | Directory where the container registers its presence. | `~/.scitex/agent-container/registry` | path |
| `SCITEX_AGENT_CONTAINER_RUNTIME_DIR` | Per-agent runtime state root for the claude-session runner (pid / heartbeat.json / session.jsonl / quota.json / session_id). | `~/.scitex/agent-container/runtime` | path |
| `SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR` | Directory for SLURM-job state handoff. | `~/.scitex/agent-container/slurm` | path |
| `SCITEX_AGENT_CACHE_DIR` | Agent-local cache directory. | `~/.cache/scitex-agent` | path |

## Credentials

Auth precedence (highest → lowest) in `runtimes/_sdk_common.py::provision_anthropic_auth`:

1. `ANTHROPIC_API_KEY` already in env (caller pre-set; SDK uses it as-is).
2. `~/.claude/.credentials.json` Pro/Max OAuth (preferred — no per-token billing).
3. `SAC_ANTHROPIC_API_KEY` (sac-namespaced handoff). Accepts either form — `sk-ant-oat*` is synthesised back into a credentials file (OAuth path), `sk-ant-api*` is bridged straight to `ANTHROPIC_API_KEY`.

**Spartan compute-node gotcha (2026-05-03):** the user's `~/.bash.d/secrets/` exports `SAC_ANTHROPIC_API_KEY` from `~/.claude/.credentials.json`. If the credentials file is stale, the runner can fail with "401 Invalid auth" or "Command failed exit 1". Fix: `unset SAC_ANTHROPIC_API_KEY` (or refresh credentials with `claude /login`) in the wrapper that starts the runner on Spartan.

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Read directly by the SDK if pre-set. The runner does NOT export this; the SDK calls `claude` CLI which falls back to `~/.claude/.credentials.json` OAuth when this is unset. | `—` | string |
| `SAC_ANTHROPIC_API_KEY` | Sac-namespaced auth handoff. Accepts both OAuth (`sk-ant-oat*`) and API-key (`sk-ant-api*`) forms; runner detects by prefix. Local shells populate via `sac dev credential2apikey`; CI populates via the GitHub Actions secret of the same name (rotate with `sac dev upload-apikey-from-credentials-to-github`). | `—` | string |
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
| `SAC_ACTION_RETENTION_DAYS` | How many days to keep action logs. | `30` | int |
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
