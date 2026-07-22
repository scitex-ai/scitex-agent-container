---
description: |
  [TOPIC] Python programmatic surface
  [DETAILS] AgentConfig + load_config/validate_config + the `agent` namespace (start/stop/restart/status/logs/...) + Registry + peer.post_turn for outbound A2A. Use when embedding sac in another Python process (orochi master, scripts, notebooks).
tags: [scitex-agent-container-python-api]
---

# Python API

## Lifecycle

The lifecycle verbs live under the `agent` namespace and take an agent
**name** (or YAML path), not a config object. Each returns a JSON-friendly
`dict`.

```python
from scitex_agent_container import agent

agent.start("worker")                     # daemon — returns once PID file lands
agent.start("worker", foreground=True)    # streams stdio; blocks until runner exits

state = agent.status("worker")            # → dict (heartbeat, sdk_session, quota, ...)
print(state["sdk_session"]["quota"])

print(agent.logs("worker", lines=50))     # rendered transcript

agent.restart("worker")                   # stop + start, preserving session_id resume
agent.stop("worker")                      # SIGTERM, escalate to SIGKILL after 5s

agent.health("worker")                    # heartbeat freshness + restart policy
agent.check("worker")                     # preflight: validate yaml + probe runtime deps
agent.find("quality")                     # locate agents by capability label
```

`load_config(name_or_path)` (below) accepts either an agent name (resolved
via the discovery chain — project-local → home → env → fleet dirs) or an
explicit YAML path, and returns an `AgentConfig`. `validate_config(path)`
returns a list of schema-violation strings (empty when valid).

## Peer — drive another agent's `/v1/turn`

For agent-to-agent communication (orochi master → workers, peer collaboration):

```python
from scitex_agent_container.peer import post_turn, post_turn_to_url, resolve_peer_url

# By agent name — auto-resolves URL from spec.host (or spec.hosts) + spec.a2a.port
reply = post_turn("worker", "summarize today's commits")

# By URL — useful for ad-hoc peers
reply = post_turn_to_url("http://127.0.0.1:18888/v1/turn", "hello")
reply = post_turn_to_url("ssh://mba:18890/v1/turn", "hi")  # ssh-as-transport

# Inspect resolution without sending
print(resolve_peer_url("head-mba"))
# → "ssh://mba:18890/v1/turn"   (remote → ssh transport, loopback agent stays secure)
# → "http://127.0.0.1:18888/v1/turn"   (local)
```

`post_turn` raises `peer.PeerError` on resolution / transport failure with the server's error message included.

## Registry

```python
from scitex_agent_container import Registry

reg = Registry()
for name in reg.list_agents():
    print(name, reg.get(name).runtime)
```

The registry is the persistent source of truth for "who's currently running on this host"; lifecycle calls maintain it automatically.

## Config types

```python
from scitex_agent_container import AgentConfig

cfg: AgentConfig = load_config("worker")
cfg.runtime          # "apptainer"
cfg.model            # str
cfg.expanded_workdir # str (~ resolved)
cfg.a2a.port         # int | None
cfg.hosts_spec.host  # str | list[str] ("" if unpinned) — spec.remote was deleted in WI-6
cfg.startup_commands # list[StartupCommand]
```

`AgentConfig` is a dataclass; mutation is supported but generally avoided in favor of editing the YAML.

## Discovery

```python
from scitex_agent_container.config._resolve import resolve_config

path = resolve_config("worker")  # → /path/to/worker.yaml or raises FileNotFoundError
```

## See also

- [04_cli-reference.md](04_cli-reference.md) — CLI surface (mirror of these calls)
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` (peer ↔ runner protocol)
- [13_observability.md](13_observability.md) — `agent_status` JSON shape
- [15_claude-session.md](15_claude-session.md) — runtime-specific behavior
