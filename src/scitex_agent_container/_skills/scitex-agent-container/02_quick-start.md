---
description: |
  [TOPIC] First agent in 30 seconds
  [DETAILS] Minimal YAML + sac start + sac show-status + sac stop. Two flavors: local SDK agent and remote agent on mba/spartan via ssh.
tags: [scitex-agent-container-quick-start]
---

# Quick Start

## Local SDK agent (30 seconds)

```bash
mkdir -p ~/.scitex/agent-container/agents/hello/
cat > ~/.scitex/agent-container/agents/hello/hello.yaml <<'EOF'
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: claude-session
  model: claude-haiku-4-5
  workdir: /tmp/hello-workspace
  startup_commands:
    - command: "Reply with the string 'hello-ok' and nothing else."
EOF

sac start hello --foreground       # streams assistant chunks to stdout, exits when done
```

Expected: `hello-ok` appears in your terminal, runner exits with rc=0.

## Long-running agent with HTTP inbound

```yaml
# ~/.scitex/orochi/shared/agents/worker/worker.yaml
spec:
  runtime: claude-session
  model: claude-haiku-4-5
  workdir: /tmp/worker
  a2a:
    port: 18888                    # enables POST /v1/turn
```

```bash
sac start worker                    # daemon mode (returns once runner writes its PID)
sac show-status worker              # heartbeat + sdk_session block
curl -sX POST http://127.0.0.1:18888/v1/turn \
     -H 'Content-Type: application/json' \
     -d '{"text": "what is 2+2?"}'
# → {"reply": "4", "exit_after": false}
sac show-logs worker                # rendered transcript
sac stop worker                     # graceful SIGTERM
```

## Remote agent on another host

```yaml
spec:
  runtime: claude-session
  model: claude-haiku-4-5
  workdir: /tmp/head-mba
  remote:
    host: mba                       # ssh alias
    user: ywatanabe
  a2a:
    port: 18890                     # loopback on remote — unreachable from outside ssh
```

```bash
sac start head-mba                  # ssh → render bash → runner survives ssh disconnect
sac show-status head-mba           # ssh-reads remote state
sac show-logs head-mba             # ssh-tails remote session.jsonl
sac stop head-mba                   # ssh + SIGTERM remote pid
```

Drive a remote agent's `/v1/turn` from Python:

```python
from scitex_agent_container.peer import post_turn
reply = post_turn("head-mba", "summarize today's commits")
# → ssh tunnel + curl on remote (loopback stays loopback)
```

## See also

- [03_python-api.md](03_python-api.md) — full programmatic surface
- [04_cli-reference.md](04_cli-reference.md) — every CLI subcommand
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [16_claude-session-migration.md](16_claude-session-migration.md) — flipping an existing claude-code agent to claude-session
