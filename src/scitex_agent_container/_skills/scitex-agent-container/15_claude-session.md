---
description: |
  [TOPIC] The claude-session SDK runner (inside apptainer)
  [DETAILS] sac runs claude-agent-sdk via the claude_session runner INSIDE an apptainer SIF (sac-base.sif + relaxed directory overlay + uv-editable /opt/venv-agent). `runtime: apptainer` is the only operative runtime value; `claude-session` is the name of the in-container runner module, NOT a runtime you select. Covers state layout, auth, turn endpoint, status JSON.
tags: [scitex-agent-container-claude-session, claude-session, sdk]
---

# The claude-session SDK runner (inside apptainer)

`claude-session` is the **name of the in-container SDK runner module**
(`scitex_agent_container._runners.claude_session`), driven by
`claude-agent-sdk`. It is **not** a runtime you select in YAML.

sac is **apptainer-only** (since 2026-05-13 — docker/podman ripped out).
`runtime: apptainer` is the only value the validator accepts
(`config/_validation.py` rejects everything else). When `sac agents start`
runs an agent, `ClaudeSessionRuntime` delegates to
`ApptainerContainerRuntime`, which `apptainer exec`s the runner **inside
the SIF** — "The host side never spawns a Python subprocess; every
`start` goes through `apptainer exec`" (`runtimes/claude_session.py`).

The canonical container shape (see [24_image-build.md](24_image-build.md)
and [25_claude-setup-delivery.md](25_claude-setup-delivery.md)):

- **SIF**: `sac-base.sif` (OS + dev tools + uv + node). There is no
  separate `sac-scitex.sif`.
- **Overlay**: a relaxed directory overlay (`--overlay <dir>/`, NOT an
  `.img`) holds the writable upper layer that persists across restarts.
- **Code**: a uv-editable venv at `/opt/venv-agent`, bootstrapped once via
  `uv pip install -e ".[all]"` from the repo mounted at `/work`, persisted
  in the overlay. The package code the agent runs comes from this editable
  venv, not from a pre-baked SIF layer.

The SDK runner does not use a terminal multiplexer (no tmux/screen, no
pane scraping, no auto-accept) — but it still runs **inside the
container**, not as a bare host process. The `spec.multiplexer` field is
vestigial for agents (see [02_multiplexer.md](02_multiplexer.md)).

Same lifecycle CLI surface across every agent (`sac agents start`,
`sac agents stop`, `sac agents status`, `sac agents tail`).

## What the SDK runner gives you

| Concern | Behaviour |
|---|---|
| Process | `claude_session` runner inside `apptainer exec` (no multiplexer) |
| TUI prompts | none — `permission_mode='bypassPermissions'` (no auto-accept needed) |
| Hooks | Python async callbacks (`_runners/_session_hooks.py`) |
| Resume | `ClaudeAgentOptions(resume=...)` auto-loaded from `state_dir/session_id` |
| Quota | accumulated from per-turn `usage` blocks in the SDK message stream |
| Auth | `~/.claude/.credentials.json` OAuth (flat-rate) or `SAC_ANTHROPIC_API_KEY` — see below |
| Human attach | `--foreground` / `sac agents tail` |

## Minimal YAML (canonical apptainer pattern)

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels: { project: my-project }

spec:
  runtime: apptainer
  workdir: /home/me/proj/my-project

  apptainer:
    image: /home/me/.scitex/agent-container/containers/sac-base.sif
    relaxed: true
    raw_args:
      - --userns
      - --containall
      - --home
      - /home/agent
      - --overlay
      - /home/me/.scitex/agent-container/containers/overlays/my-agent/

  claude:
    model: claude-haiku-4-5

  startup_commands:
    # Idempotent venv bootstrap — only builds if missing (overlay persists it
    # across restarts), so boots don't re-run a full install every time.
    - command: '[ -x /opt/venv-agent/bin/python ] || { cd /work && uv venv /opt/venv-agent --python python3 && uv pip install --python /opt/venv-agent/bin/python -e ".[all]"; }'

  startup_prompts:
    - "Reply with the string 'hello' and nothing else."
```

`startup_commands` are SHELL commands run inside the container **before**
the SDK starts (e.g. the `/opt/venv-agent` bootstrap above).
`startup_prompts` carry the claude mission text. `delay` is ignored — the
SDK takes a one-shot prompt, not a timed sequence.

## Operating modes

### Daemon (default — production fleet shape)

```bash
sac agents start my-agent          # detach, returns once PID file lands
sac agents status my-agent    # heartbeat + sdk_session block
sac agents tail my-agent      # rendered transcript from session.jsonl
sac agents stop my-agent
```

### Foreground (interactive — terminal visibility)

```bash
sac agents start my-agent --foreground
# assistant output streams to stdout; runner exits when the turn completes
```

`--foreground` is single-target only; multi-target / directory targets
are rejected since the runner takes over the terminal.

## Inbound-turn HTTP endpoint

Add `spec.a2a.port` and the runner serves `POST /v1/turn` colocated
with the SDK conversation — turns land on the same `ClaudeSDKClient`
so resume id + quota are preserved. See
[17_inbound-turn-endpoint.md](17_inbound-turn-endpoint.md) for wire
format, semantics, ssh-as-transport for remote agents, and the
`SAC_RUNNER_PREFIX` hook.

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

The `sdk-runtime-smoke` GitHub workflow runs this exact command on every
push to develop / main (and daily) so an upstream SDK breakage surfaces
immediately rather than during the next manual fleet operation.

## Supervisor — auto-restart on SDK crash

`--max-restarts N` (default `0` = terminate on first failure) + `--restart-backoff-s S` (default `1.0`, doubles per attempt) let the runner reopen `ClaudeSDKClient` after a mid-session exception. On each retry it re-reads `session_id` from the state dir so the new client resumes the latest completed turn; the inbox is preserved across restarts. Each restart writes `{type: error, kind: sdk_runtime, attempt: N}` + `{type: supervisor, event: restarting, in_s: <delay>}` to `session.jsonl`. After `max_restarts` attempts the inbox is drained with the last exception and the runner exits. Init failures (missing SDK, bad options) stay terminal.

## Constraints

- **No human-typed interactive session.** There is no tmux pane to type
  `/clear`, `/compact`, or paste into. The SDK runner takes one mission
  prompt at start; subsequent turns arrive via A2A (`POST /v1/turn`,
  `sac peer post-turn`, or another inbound channel).

## Related skills

- [24_image-build.md](24_image-build.md) — building/rebuilding the
  `sac-base.sif` the runner executes inside.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — how
  `to_home/`, settings, MCP, and credentials reach the in-container `$HOME`.
- [03_auto-accept.md](03_auto-accept.md) — the auto-accept handler set is
  vestigial for the SDK runner (it has no TUI prompts).
- [13_observability.md](13_observability.md) — the broader status JSON
  contract; this leaf describes the SDK-specific addition.
