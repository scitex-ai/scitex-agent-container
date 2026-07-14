---
description: |
  [TOPIC] claude-session runner — state layout, auth, status JSON, supervisor
  [DETAILS] Operational reference for the in-container claude-session SDK runner (see 15_claude-session.md for the overview). Covers the per-agent state dir (`pid`/`heartbeat.json`/`session.jsonl`/`session_id`/`quota.json`), scope resolution, the cost-critical auth precedence (`~/.claude/.credentials.json` OAuth vs `SAC_ANTHROPIC_API_KEY`), the `sdk_session` status-JSON block, the in-repo smoke test, the auto-restart supervisor, and the no-interactive-session constraint.
tags: [scitex-agent-container-claude-session-state, claude-session, sdk]
---

# claude-session runner — state, auth, status, supervisor

Operational reference for the in-container `claude-session` SDK runner.
For the runtime overview, the canonical apptainer YAML, and operating
modes, see [15_claude-session.md](15_claude-session.md).

## State layout

Per-agent state lives at `<scope>/runtime/<name>/`:

| File | Contents |
|---|---|
| `pid` | Runner's PID (atomic write — tmp + rename). |
| `heartbeat.json` | `{ts, pid, state}` plus `elapsed_s` (seconds since session start, from `started_at`) and the running token totals `input_tokens / output_tokens / total_tokens` (from `quota.json`). State ∈ `starting / idle / working / stopping`. Refreshed every 10 s (`--tick-seconds`). |
| `started_at` | Session start time (unix seconds). Written once at startup; preserved across a resumed respawn so `elapsed_s` tracks the conversation, not the process. |
| `session.jsonl` | One JSON object per turn event: `user / assistant / user_echo / result / error`. The transcript. |
| `session_id` | Latest SDK session UUID. Auto-resumed by the next `sac agents start`. |
| `quota.json` | Accumulated per-turn token totals (input / output / cache_creation / cache_read / turns). |

Scope resolution (highest priority first):

1. **Project-local**: walks up from cwd to a git repo containing
   `.scitex/agent-container/`. State lands in
   `<repo>/.scitex/agent-container/runtime/<name>/`.
2. **Home**: `~/.scitex/agent-container/runtime/<name>/` (when no
   project scope is available).
3. Override via `$SCITEX_AGENT_CONTAINER_RUNTIME_DIR` (the runner reads
   this verbatim — useful for ad-hoc tests).

Symmetric: agent definitions follow the same resolution order
(`<scope>/agents/<name>/<name>.yaml` first, then
`~/.scitex/agent-container/agents/`, then env, then fleet dirs).

## Auth (cost-critical)

The SDK's authentication path, by precedence
(`runtimes/_sdk_common.py::provision_anthropic_auth`):

1. `~/.claude/.credentials.json` exists → SDK reads OAuth token
   automatically (Pro/Max plan, **flat-rate**). The default on every
   workstation that has run `claude /login`.
2. `SAC_ANTHROPIC_API_KEY` (sac-namespaced handoff for headless
   contexts — CI, SLURM, cron). When set it is mirrored into
   `ANTHROPIC_API_KEY` for the SDK (an `sk-ant-api*` value is
   **pay-per-token**, explicit opt-in only).

A bare host `ANTHROPIC_API_KEY` is **never honoured**: the first thing
`provision_anthropic_auth` does is overwrite it from
`SAC_ANTHROPIC_API_KEY`, or *pop* it from the env when
`SAC_ANTHROPIC_API_KEY` is unset — so a stale dotfiles export can't be
picked up by the SDK auto-reader, shadow a working OAuth credentials
file, or silently switch you to API-key billing. Set neither input and
you get a clear `SDKCommonError`.

## Status JSON addition

`sac agents status <name> --json` carries an `sdk_session` field for
agents on this runtime:

```json
{
  "runtime": "apptainer",
  "sdk_session": {
    "session_id": "6ef8248f-1ccd-4877-934d-908e15333b52",
    "quota": {
      "input_tokens": 6,
      "output_tokens": 14,
      "cache_creation_input_tokens": 33003,
      "cache_read_input_tokens": 0,
      "turns": 1
    },
    "heartbeat": {
      "ts": 1777766006.95, "pid": 3476972, "state": "idle",
      "started_at": 1777765800.0, "elapsed_s": 206.95,
      "input_tokens": 6000, "output_tokens": 1500, "total_tokens": 40503
    },
    "state_dir": "/path/to/.scitex/agent-container/runtime/<name>"
  }
}
```

`sdk_session` is populated for `kind: Agent` (SDK runner) and `null` for
`kind: AgentProxy` (the HTTP forwarder, which has no SDK) — never absent
— so dashboards can switch on its presence without parsing further.

## In-repo smoke test

`<repo>/.scitex/agent-container/agents/sdk-test/sdk-test.yaml` is a
checked-in fixture that the project-local discovery picks up
automatically when `sac` is invoked from inside the repo:

```bash
cd ~/proj/scitex-agent-container
sac agents start sdk-test --foreground
# expected: sdk-runtime-ok
```

Run it by hand — there is NO CI safety net behind it. A `sdk-runtime-smoke`
workflow used to claim it ran this on every push; it was disabled at the
docker ripout, never executed once, and has been deleted. An upstream SDK
breakage surfaces at the next manual fleet operation, not in CI.

## Supervisor — auto-restart on SDK crash

`--max-restarts N` (default `0` = terminate on first failure) + `--restart-backoff-s S` (default `1.0`, doubles per attempt) let the runner reopen `ClaudeSDKClient` after a mid-session exception. On each retry it re-reads `session_id` from the state dir so the new client resumes the latest completed turn; the inbox is preserved across restarts. Each restart writes `{type: error, kind: sdk_runtime, attempt: N}` + `{type: supervisor, event: restarting, in_s: <delay>}` to `session.jsonl`. After `max_restarts` attempts the inbox is drained with the last exception and the runner exits. Init failures (missing SDK, bad options) stay terminal.

## Constraints

- **No human-typed interactive session.** There is no tmux pane to type
  `/clear`, `/compact`, or paste into. The SDK runner takes one mission
  prompt at start; subsequent turns arrive via A2A (`POST /v1/turn`,
  `sac peer post-turn`, or another inbound channel).

## Related skills

- [15_claude-session.md](15_claude-session.md) — the runtime overview,
  canonical apptainer YAML, and operating modes (this leaf is its
  state/auth/status reference half).
- [24_image-build.md](24_image-build.md) — building/rebuilding the
  `sac-base.sif` the runner executes inside.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — how
  `to_home/`, settings, MCP, and credentials reach the in-container `$HOME`.
- [03_auto-accept.md](03_auto-accept.md) — the auto-accept handler set is
  vestigial for the SDK runner (it has no TUI prompts).
- [13_observability.md](13_observability.md) — the broader status JSON
  contract; this leaf describes the SDK-specific addition.
