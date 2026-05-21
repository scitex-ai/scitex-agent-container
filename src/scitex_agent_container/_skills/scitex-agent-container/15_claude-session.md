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

## State, auth, status, supervisor

The operational reference — per-agent state-dir layout
(`pid`/`heartbeat.json`/`session.jsonl`/`session_id`/`quota.json`),
scope resolution, the cost-critical auth precedence, the `sdk_session`
status-JSON block, the in-repo smoke test, the auto-restart supervisor,
and the no-interactive-session constraint — lives in the sibling leaf
[14_claude-session-state.md](14_claude-session-state.md).

## Related skills

- [14_claude-session-state.md](14_claude-session-state.md) — state
  layout, auth, status JSON, supervisor (this overview's reference half).
- [24_image-build.md](24_image-build.md) — building/rebuilding the
  `sac-base.sif` the runner executes inside.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — how
  `to_home/`, settings, MCP, and credentials reach the in-container `$HOME`.
- [03_auto-accept.md](03_auto-accept.md) — the auto-accept handler set is
  vestigial for the SDK runner (it has no TUI prompts).
- [13_observability.md](13_observability.md) — the broader status JSON
  contract; the state leaf describes the SDK-specific addition.
