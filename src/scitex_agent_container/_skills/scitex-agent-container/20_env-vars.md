---
description: |
  [TOPIC] scitex-agent-container — Environment Variables (behavior + gotchas)
  [DETAILS] The two-name aliasing rule + SacEnvConflict, auth precedence, hub fail-soft, the in-SIF listen-broker fail-loud, the compaction opt-out, and the Spartan stale-cred gotcha. The full ~40-var enumeration is derivable from source — run the audit grep; this leaf does NOT table every var.
tags: [scitex-agent-container-env-vars]
---

# scitex-agent-container — Environment Variables

sac reads ~40 env vars. The **exhaustive list is derivable from
source**, so this leaf keeps only the behaviors and gotchas you can't
read off a name. For the authoritative current list:

```bash
grep -rhoE 'SCITEX_[A-Z0-9_]+|SAC_[A-Z0-9_]+' \
  "$HOME/proj/scitex-agent-container/src/" | sort -u
```

The reader/alias/conflict logic lives in
`scitex_agent_container._env`.

## `SAC_*` and `SCITEX_AGENT_CONTAINER_*` are interchangeable

Every sac-owned var has two equivalent names — a short `SAC_<X>` and a
long `SCITEX_AGENT_CONTAINER_<X>`. Both read the same slot via
`_env.getenv("X")`.

**Gotcha:** if both forms are set to **different** values, sac raises
`SacEnvConflict` at startup rather than silently picking one — a
drifted alias is almost always a bug. Same value on both forms is fine.

## Credentials — the precedence that decides which auth wins

`runtimes/_sdk_common.py::provision_anthropic_auth`, highest → lowest:

1. `ANTHROPIC_API_KEY` already in env — SDK uses it as-is.
2. `~/.claude/.credentials.json` Pro/Max OAuth — preferred (no
   per-token billing).
3. `SAC_ANTHROPIC_API_KEY` (sac-namespaced handoff). `sk-ant-oat*` is
   synthesised back into a credentials file (OAuth path); `sk-ant-api*`
   is bridged straight to `ANTHROPIC_API_KEY`.

**Spartan stale-cred gotcha (2026-05-03):** the user's
`~/.bash.d/secrets/` exports `SAC_ANTHROPIC_API_KEY` from
`~/.claude/.credentials.json`. If that file is stale the runner fails
with "401 Invalid auth" / "Command failed exit 1". Fix: `unset
SAC_ANTHROPIC_API_KEY` (or `claude /login` to refresh) in the wrapper
that starts the runner.

## Hub integration is fail-soft

sac is fleet-agnostic. Set `SAC_HUB_URL` (long: `…_HUB_URL`) +
`…_HUB_TOKEN` to join a fleet hub. **No default** — unset ⇒ standalone,
hub calls skipped; set-but-unreachable ⇒ log and continue. sac never
hard-fails on hub absence. Downstream fleets (orochi) own their own env
namespace; sac does not read fleet-specific vars.

## In-SIF listen-broker is fail-loud

When an agent runs inside an Apptainer SIF, `sac agents start <child>`
can't `apptainer exec` locally (no nested apptainer on the supported
HPC shape). The runtime injects `SAC_LISTEN_BASE_URL` (+ `SAC_LISTEN_BEARER`)
so the in-SIF CLI POSTs the spawn RPC to the bare host, which re-runs
`check_spawn`, records the `caller → child` lineage edge, and shells
the real start against the host's apptainer.

**Contract:** in a SIF with `SAC_LISTEN_BASE_URL` unset, `sac agents
start` raises `InSifBrokerError` — it never silently downgrades to
"skip the broker" or "try local apptainer anyway". `SAC_LISTEN_BEARER`
is required when `server:sac` is in `spec.claude.channels` (fails loud
at launch otherwise).

## Var groups (for orientation only — see the audit grep for the list)

Container identity/metadata · paths (config / registry / runtime /
cache) · credentials · context-compaction knobs (`SAC_COMPACT_*`) ·
probe/heartbeat timing (`SAC_PROBE_*`, `SCITEX_HEARTBEAT_INTERVAL`) ·
read-only hook context (`SCITEX_HOOK*`, set by the harness) · hub ·
listen-broker.

## Feature flags

- Opt-out: `SAC_COMPACT_ENABLED=false` disables context compaction.
- No opt-in flags in this package.
