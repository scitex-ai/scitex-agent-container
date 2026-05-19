---
description: |
  [TOPIC] HTTP surface — `POST /v1/turn` inbound endpoint
  [DETAILS] Wire format, semantics, ssh-as-transport for remote agents, error codes. The full reference (curl examples, comparison vs legacy A2A sidecar, implementation files, SAC_RUNNER_PREFIX hook) lives in 17_inbound-turn-endpoint.md.
tags: [scitex-agent-container-http-api]
---

# HTTP API

Sac ships exactly two HTTP routes per agent (when `spec.a2a.port` is set):

| Method + Path | Purpose |
|---|---|
| `POST /v1/turn` | Send one user turn to the runner's persistent SDK conversation; receive the assistant reply. Serial — turns queue. |
| `GET /health` | Liveness probe → `{"status": "ok"}`. |

## Wire format

```bash
# Local agent
curl -sX POST http://127.0.0.1:18888/v1/turn \
  -H 'Content-Type: application/json' \
  -d '{"text": "your message", "exit_after": false}'
# 200 OK → {"reply": "...", "exit_after": false}
```

Request body fields:

| Field | Type | Default | Required |
|---|---|---|---|
| `text` | string | — | yes (non-empty) |
| `exit_after` | bool | `false` | no — when `true` the runner shuts down after this turn |

Response body fields:

| Field | Type | Notes |
|---|---|---|
| `reply` | string | Concatenated `AssistantMessage.TextBlock` chunks for the turn |
| `exit_after` | bool | Echo of the request flag |

## Status codes

| Code | When |
|---|---|
| 200 | Turn completed; `reply` carries the assistant text |
| 400 | Missing or empty `text`, or malformed JSON |
| 502 | Runtime error from the SDK (`turn failed: <detail>`); details also in `session.jsonl` (`type: error, kind: sdk_runtime`) |
| 504 | Per-turn timeout (default 600 s); SDK hung |

## Remote agents — ssh-as-transport

For agents declared with `spec.remote.host`, the runner stays on `127.0.0.1` (loopback only — no LAN exposure). Peers reach it through ssh:

```python
from scitex_agent_container.peer import post_turn
reply = post_turn("head-mba", "your message")
# resolves to ssh://mba:18888/v1/turn → ssh + curl on remote
```

This works for ssh aliases that aren't DNS-resolvable from the caller (e.g., `mba`, `head-spartan`) and survives NAT.

## Concurrency / timeouts

- **Serial drain**: a new POST waits for the previous turn's `receive_response()` to finish before the SDK is queried again. Matches Claude Code's "next prompt waits" UX.
- **Per-turn cap**: 600 s (configurable via the runner's `turn_timeout_s`); the SDK call itself isn't capped — the cap is local to the HTTP handler.

## Host-wide control plane — `sac listen`

`/v1/turn` is the **per-agent** wire (one HTTP server per long-lived runner, bound to `spec.a2a.port`). The **host-wide control plane** is a separate process: `sac listen` exposes `/v1/sac/{health,agents,agents/<name>/{status,send,card}}` for orchestrators (e.g. orochi) to reach every agent on the host through one bearer-authenticated endpoint. `POST /agents/<name>/send` forwards turns into the live runner's `/v1/turn` when `spec.a2a.port` is set; otherwise it falls back to `claude --resume <sid> -p`. With `Accept: text/event-stream` it streams claude's stream-json output as SSE frames. See `10_cli.md` for the full route table.

## See also

- [10_cli.md](10_cli.md) — `sac listen` + `sac agent send` + `sac peer post-turn`
- [17_inbound-turn-endpoint.md](17_inbound-turn-endpoint.md) — full reference: detailed wire examples, comparison vs legacy A2A sidecar, implementation files, `SAC_RUNNER_PREFIX` hook for SLURM / apptainer wrappers
- [03_python-api.md](03_python-api.md) — `peer.post_turn()` + `peer.PeerError`
- [07_a2a-protocol.md](07_a2a-protocol.md) — JSON-RPC `message/send` surface (legacy `runtime: claude-code` only)
