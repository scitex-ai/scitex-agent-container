---
description: |
  [TOPIC] Inbound-turn HTTP endpoint (`POST /v1/turn`)
  [DETAILS] Reference for the in-runner HTTP inbound-turn endpoint served by the claude-session runtime when ``spec.a2a.port`` is declared. Wire format, semantics, curl examples, and how it differs from the legacy A2A sidecar..
tags: [scitex-agent-container-inbound-turn-endpoint, claude-session, a2a, inbound]
---

# Inbound-turn HTTP endpoint (`POST /v1/turn`)

Long-living `runtime: claude-session` agents accept new turns over HTTP. The endpoint is **colocated with the SDK conversation** (no sidecar process) so each turn lands on the same persistent `ClaudeSDKClient` — the resume id, accumulated quota, and tool history are preserved across turns.

Lives in `_runners/_session_http.py`; spawned by `_runners/claude_session.py::run` when the runner argv has `--a2a-port N` (the runtime adapter sets that from `spec.a2a.port`).

## YAML

```yaml
spec:
  runtime: claude-session
  a2a:
    port: 18888         # required to enable the endpoint
    host: 127.0.0.1     # default; set to 0.0.0.0 for LAN exposure
```

Existing `claude-code` agents that already have an `a2a.port` (e.g.
handyman-sonnet at 19108) keep the same port — when you flip
`runtime: claude-code` → `claude-session`, the runner's HTTP server
binds the same port the sidecar used to. Telegram bridge / orochi
clients keep working unchanged.

## Wire format

```bash
# One turn → one assistant reply
curl -sX POST http://127.0.0.1:18888/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "summarize today commits", "exit_after": false}'
# 200 → {"reply": "...", "exit_after": false}

# Tell the runner to shut down after this turn (CI smokes use this)
curl -sX POST http://127.0.0.1:18888/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "echo done", "exit_after": true}'

# Liveness
curl -s http://127.0.0.1:18888/health
# 200 → {"status": "ok"}
```

## Semantics

- **Serial**, not interleaved. A new POST waits until the prior turn's `receive_response()` drains. Matches Claude Code's own UX (next prompt waits).
- **Per-turn timeout: 600 s.** SDK hangs surface as `504` with `{"error": "turn timeout after 600s"}`.
- **Validation:** missing or empty `text` → `400`.
- **Errors:** SDK runtime errors surface as `502` with `{"error": "turn failed: <detail>"}` and the same envelope is appended to `session.jsonl` (kind: `sdk_runtime`).

## How it differs from the legacy A2A sidecar

| Aspect | Legacy sidecar (`runtime: claude-code`) | In-runner (`runtime: claude-session`) |
|---|---|---|
| Process | Separate `sac a2a serve` process | Asyncio task inside the runner |
| Per-request transport | New `query()` per request — fresh conversation each time | `client.query()` on the persistent SDK client — turns share context |
| Wire | A2A JSON-RPC `message/send` | Plain `{text, exit_after}` (PR4 will add JSON-RPC compat) |
| Concurrency | Each request spawns its own SDK call | Serial drain — turns queue |
| Resume | None (each request is stateless) | Full — session_id persists across runs |

## Wiring details

The runner's argv `--a2a-port`/`--a2a-host` is set automatically by `runtimes/claude_session.py::start` from `spec.a2a.port` / `spec.a2a.host`. The handler enqueues a `TurnEnvelope` on the runner's `asyncio.Queue` and awaits `env.response`; the conversation task drains it, calls `client.query(text)`, drains `receive_response()`, and resolves the future.

## Implementation files

- `src/scitex_agent_container/_runners/_session_http.py` — Starlette app + uvicorn task
- `src/scitex_agent_container/_runners/_session_inbox.py` — `TurnEnvelope` / `ShutdownEnvelope` / `make_inbox()`
- `src/scitex_agent_container/_runners/claude_session.py::_run_conversation` — drains the inbox into the persistent `ClaudeSDKClient`
- `tests/scitex_agent_container/_runners/test__session_http.py` — round-trip + 400 + health smoke tests

## Remote launch via `_remote_launch.render_remote_launch`

For running the SDK runner on a remote host, sac provides a generic bash-script generator that sources a per-host hook before exec. The package stays generic; per-host quirks (Spartan module loads, NAS PATH overrides, etc.) live in private `~/.scitex/agent-container/hosts/$(hostname).sh` on the remote.

```python
from scitex_agent_container._runners._remote_launch import render_remote_launch

script = render_remote_launch(
    runner_argv=["python", "-m", "scitex_agent_container._runners.claude_session",
                 "--name", "my-agent", "--a2a-port", "18888"],
    agent_name="my-agent",
    state_root="/tmp/runtime",
    detach=True,  # setsid + nohup; emits the runner PID on stdout
)
# Pipe over ssh:
#   ssh -o BatchMode=yes <host> 'bash -l -s' <<< "$script"
# The login shell ensures Lmod / pyenv / venv-PATH from .bashrc is loaded
# *before* the per-host hook runs.
```

The script:

1. (if `state_root` given) `export SCITEX_AGENT_CONTAINER_RUNTIME_DIR=...`
2. Source `~/.scitex/agent-container/hosts/$(hostname).sh` if it exists (silent skip otherwise)
3. exec the runner (foreground) or `setsid nohup ... &` (detached) with output redirected to `runner.log`

**Always invoke remote with `bash -l -s` (login shell)** so the user's `.bashrc` loads (Lmod, venv PATH, etc.) before the hook runs. Tested 2026-05-03 on `spartan-bm198`: hook does `module load GCCcore/11.3.0 OpenSSL/1.1; unset SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY` → SDK runner round-trips a turn against the OAuth in `~/.claude/.credentials.json`.

### `SAC_RUNNER_PREFIX` — generic launcher hook

The launch script honors `${SAC_RUNNER_PREFIX:-}` immediately before the runner argv. Per-host hooks can set this to wrap the runner with **anything**:

```bash
# ~/.scitex/agent-container/hosts/spartan-bm198.hpc.unimelb.edu.au.sh
# Spartan: re-exec the runner inside an existing SLURM allocation
module load GCCcore/11.3.0 OpenSSL/1.1 slurm/default
unset SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY
if [ -z "$SLURM_JOB_ID" ]; then
    JOBID=$(squeue --me -h -n head-spartan -o "%i" | head -1)
    [ -n "$JOBID" ] && export SAC_RUNNER_PREFIX="srun --jobid=$JOBID --overlap"
fi

# OR: for an apptainer-pinned runner version (any host)
# export SAC_RUNNER_PREFIX="apptainer exec --bind $HOME/proj:$HOME/proj \"$HOME/scitex-images/sac-0.13.sif\""
```

This keeps SLURM, apptainer, container-runtime, conda-env-activation, etc. as **user-side concerns** — sac stays generic. The package ships a single env-var honor; users compose their own dispatch. Live-verified 2026-05-03 against `spartan-bm198`: same `sac agent start` command works on plain ssh hosts and Spartan compute nodes simultaneously.
