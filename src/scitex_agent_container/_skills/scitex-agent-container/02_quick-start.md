---
description: |
  [TOPIC] First agent in 30 seconds
  [DETAILS] Build the layered :base/:scitex SIF once, then minimal spec.yaml + sac agents start + sac agents tail + sac agents stop. Three flavors: local apptainer agent, long-running with A2A inbound, remote agent via ssh.
tags: [scitex-agent-container-quick-start]
---

# Quick Start

## 0. One-time: build the runtime images

```bash
sac image build base   -y          # OS + dev tools             (~15-25 min, one-time)
sac image build scitex -y          # FROM :base + scitex[all]   (~10-20 min with uv)
```

The `:scitex` def file uses **uv** (Rust-based parallel resolver) to
install `scitex[all]`. uv finishes in 1-3 min what plain pip would
spend 30+ min thrashing on (it walks version histories of the heavy
transitive set — sphinx-rtd-theme, openalex-local, awscli/botocore,
etc.). The def falls back to pip if uv is missing on the base layer.

Sandbox builds (writable rootfs dirs, suffix `.sandbox/`):

```bash
sac image build scitex --sandbox -y    # writable :scitex rootfs
```

Skip if you already have `scitex-agent-container-scitex.sif` from a teammate or a published release.

## 1. Local agent (30 seconds)

```bash
mkdir -p ~/.scitex/agent-container/agents/hello/
cat > ~/.scitex/agent-container/agents/hello/spec.yaml <<'EOF'
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer                  # launch mode; there is no docker option
  workdir: /tmp/hello-workspace
  model: claude-haiku-4-5
  startup_commands:
    - command: "Reply with the string 'hello-ok' and nothing else."
EOF

sac agents start hello --foreground      # streams assistant chunks; exits when done
```

Expected: `hello-ok` in your terminal, runner exits with rc=0.

## 2. Long-running agent with A2A inbound

```yaml
# ~/.scitex/agent-container/agents/worker/spec.yaml
spec:
  runtime: apptainer
  workdir: /tmp/worker
  model: claude-haiku-4-5
  a2a:
    port: 18888                       # enables POST /v1/turn
```

```bash
sac agents start  worker                 # daemon mode
sac agents status worker                 # registry + heartbeat + sdk_session block
curl -sX POST http://127.0.0.1:18888/v1/turn \
     -H 'Content-Type: application/json' \
     -d '{"text": "what is 2+2?"}'
# → {"reply": "4", "exit_after": false}
sac agents tail   worker                 # render session.jsonl (structured transcript)
sac agents stop   worker                 # graceful SIGTERM
```

## 3. Remote agent on another host

```yaml
spec:
  runtime: apptainer
  workdir: /tmp/head-mba
  model: claude-haiku-4-5
  remote:
    host: mba                           # ssh alias
    user: ywatanabe
  a2a:
    port: 18890                         # loopback on remote — unreachable from outside ssh
```

```bash
sac agents start  head-mba               # ssh → bash render → runner survives ssh disconnect
sac agents status head-mba               # ssh-reads remote state
sac agents tail   head-mba               # ssh-tails remote session.jsonl
sac agents stop   head-mba               # ssh + SIGTERM remote pid
```

Drive a remote agent's `/v1/turn` from Python:

```python
from scitex_agent_container._network.peer import post_turn
reply = post_turn("head-mba", "summarize today's commits")
# → ssh tunnel + curl on remote (loopback stays loopback)
```

## See also

- [03_python-api.md](03_python-api.md) — full programmatic surface
- [04_cli-reference.md](04_cli-reference.md) — every CLI subcommand
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
