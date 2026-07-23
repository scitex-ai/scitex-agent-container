---
description: |
  [TOPIC] scitex-agent-container — Environment Variables
  [DETAILS] Environment variables read by scitex-agent-container at import / runtime. Follow SCITEX_<MODULE>_* convention — see general/10_arch-environment-variables.md..
tags: [scitex-agent-container-env-vars]
---

# scitex-agent-container — Environment Variables

With ~40 sac-owned vars in source, this leaf groups them by purpose. For the
exact authoritative list, run the audit snippet at the bottom.

## Naming — `SAC_*` and `SCITEX_AGENT_CONTAINER_*` are interchangeable

Every sac-owned env var has TWO equivalent names: a short `SAC_<X>` form
and a long `SCITEX_AGENT_CONTAINER_<X>` form. Either reads to the same
slot. The helper at `scitex_agent_container._env.getenv("X")` reads
both.

**Conflict detection.** If both forms are set with **different** values,
sac raises `SacEnvConflict` at startup rather than silently picking one
— a drifted alias is almost always a bug:

```
SAC_HUB_URL=https://hub-a.example
SCITEX_AGENT_CONTAINER_HUB_URL=https://hub-b.example
# → SacEnvConflict: SAC_HUB_URL=...hub-a... vs SCITEX_AGENT_CONTAINER_HUB_URL=...hub-b...
```

When set to the same value, either form (or both) is accepted.

In every table below, the `Variable` column lists the long form for
readability; the short `SAC_*` alias is always available.

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
| `SAC_NAME` | Short agent identifier (used in telemetry). | auto | string |

## Paths

Canonical agent YAML location (fleet shared via dotfiles):
`~/.scitex/agent-container/agents/<name>/<name>.yaml` — the dir-as-SSoT
resolver walks per-host `<host>/agents/`, then `shared/agents/`, then
`agents/`. See `01_config-v3.md` for the full search path.

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_CONFIG_PATH` | Path to the YAML config. | bundled | path |
| `SCITEX_AGENT_CONTAINER_YAML_DIRS` | Extra dirs scanned for YAML overrides (colon-separated). | unset | string (paths) |
| `SCITEX_AGENT_CONTAINER_REGISTRY_DIR` | Directory where the container registers its presence. | `~/.scitex/agent-container/runtime/registry` | path |
| `SCITEX_AGENT_CONTAINER_RUNTIME_DIR` | Per-agent runtime state root for the claude-session runner (pid / heartbeat.json / session.jsonl / quota.json / session_id). | `~/.scitex/agent-container/runtime` | path |
| `SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR` | Directory for SLURM-job state handoff. | `~/.scitex/agent-container/slurm` | path |
| `SCITEX_AGENT_CONTAINER_ROOT` | sac's install root, as a single base. Read by `sac agents rename` (`_lifecycle._rename_plan.Layout.default`) to derive the spec / overlay / runtime / registry / state.db paths together. Resolved at CALL time, so it actually takes effect. | `~/.scitex/agent-container` | path |
| `SAC_CACHE_DIR` | Agent-local cache directory. | `~/.cache/scitex-agent` | path |

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
| `SAC_COMPACT_ENABLED` | Enable auto-compaction of context window. | `true` | bool |
| `SAC_COMPACT_THRESHOLD_PCT` | Trigger compaction at this context % used. | `80` | int |
| `SAC_COMPACT_MIN_DROP_PCT` | Minimum % that must drop per pass. | `20` | int |
| `SAC_COMPACT_MIN_INTERVAL_S` | Minimum seconds between compactions. | `300` | int |
| `SAC_COMPACT_TIMEOUT_S` | Timeout per compaction attempt. | `60` | int |

## Action / probe / heartbeat timing

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SAC_ACTION_RETENTION_DAYS` | How many days to keep action logs. | `30` | int |
| `SAC_ACTION_SNAPSHOT_MAX_CHARS` | Truncate action snapshots above this size. | `8192` | int |
| `SAC_AUTO_RESPONSE_TICK_S` | Auto-response poll interval. | `5` | int |
| `SAC_KEY_DELAY_S` | Delay between simulated keystrokes. | `0.05` | float |
| `SAC_SUBMIT_SETTLE_S` | Seconds to wait after submitting a prompt. | `1.0` | float |
| `SAC_PROBE_INTERVAL_S` | Liveness probe interval. | `30` | int |
| `SAC_PROBE_POLL_INTERVAL_S` | Fine-grained poll step inside a probe. | `1` | int |
| `SAC_PROBE_TIMEOUT_S` | Probe timeout. | `10` | int |
| `SCITEX_HEARTBEAT_INTERVAL` | Ecosystem-wide heartbeat interval (fleet-shared). | `60` | int |

## Hook context (read-only, set by the harness)

| Variable | Purpose |
|---|---|
| `SCITEX_HOOK` | Name of the hook currently running. |
| `SCITEX_HOOK_CTX_*` | Per-hook context keys (dynamic prefix). |

## Hub / fleet integration (optional)

sac is fleet-agnostic. To join a fleet hub, set `SAC_HUB_URL` (or the
long form `SCITEX_AGENT_CONTAINER_HUB_URL`) to the hub endpoint. **No
default** — when unset, sac runs as a standalone agent and skips hub
calls. When set but unreachable, sac logs and continues; it never hard-
fails on hub absence.

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SCITEX_AGENT_CONTAINER_HUB_URL` | Fleet hub endpoint. | unset (standalone) | URL |
| `SCITEX_AGENT_CONTAINER_HUB_TOKEN` | Bearer token for the hub. | `—` | string |

Downstream fleet implementations own their own env
namespace; sac does not read fleet-specific vars directly.

## Host listen — SAC-from-SAC broker

When an agent runs INSIDE an apptainer SIF, ``sac agents start <child>``
cannot ``apptainer exec`` locally (no nested apptainer on the supported
HPC shape). The runtime injects the bare-host ``sac listen`` address +
bearer into every container so the in-SIF CLI can POST the spawn RPC to
the host instead; the host re-runs ``check_spawn``, records the
``caller → child`` lineage edge, and shells the real ``sac agent
start`` against the bare host's apptainer.

| Variable | Purpose | Default | Type |
|---|---|---|---|
| `SAC_LISTEN_BASE_URL` | Host-stable ``sac listen`` base URL the in-SIF CLI POSTs spawn requests against (also used by the in-container channel adapter to subscribe to the bus). Auto-injected by the apptainer runtime from ``listen.host`` / ``listen.port`` in ``~/.scitex/agent-container/config.yaml``. | `http://127.0.0.1:7878` | URL |
| `SAC_LISTEN_BEARER` | Bearer token presented as ``Authorization: Bearer ...`` to the host listen server. Auto-injected from the host's bearer-token file; required when ``server:sac`` is in ``spec.claude.channels`` (the runtime fails loud at launch otherwise). | `—` | string |
| `SAC_INBOX_KEEPALIVE_S` | Server: seconds between `: keepalive` frames on an IDLE inbox SSE stream. A silent stream is indistinguishable from a dead one, which parks the subscriber forever (silent deafness) — so a bad value falls back to the default rather than disabling the beat. | `15` | float |
| `SAC_MCP_SSE_READ_TIMEOUT_S` | Client (`sac mcp channel`): seconds of silence before the inbox read is declared dead and the adapter re-dials. Keep well above `SAC_INBOX_KEEPALIVE_S`. Never unbounded — "wait forever" is the bug, not a setting. | `60` | float |

Deploy order: restart `sac listen` when shipping the beat. A NEW adapter against
a not-yet-restarted daemon gets no beats and re-dials every ~60s — lossless and
self-healing (rows replay on connect), but it looks like flapping.

Fail-loud: when the broker runs in a SIF and ``SAC_LISTEN_BASE_URL`` is
unset, ``sac agents start`` raises ``InSifBrokerError`` (apptainer
runtime forgot to inject it). Never silently downgrades to "skip the
broker" / "try local apptainer anyway".

## Feature flags

- **opt-out:** `SAC_COMPACT_ENABLED=false` disables context compaction.
- No opt-in flags in this package.

## Audit

```bash
grep -rhoE 'SCITEX_[A-Z0-9_]+' $HOME/proj/scitex-agent-container/src/ | sort -u
```
