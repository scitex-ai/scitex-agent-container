# ADR-0004: Adopt A2A v1.0 (drop `/v1/` REST prefix, v1 AgentCard shape) (2026-05-14)

**Status:** Accepted.
**Supersedes:** Earlier `/v1/sac/...` REST surface + v0.3-shaped AgentCard.
**Related:** [`0001`](0001-isolation-hardening.md), [`0003`](0003-runtime-home-directory.md).

## Problem

A2A reached v1.0 stable (Linux Foundation, April 2026) and the REST
binding now **prohibits** any path that starts with `/v1/`. sac's
existing routes — `/v1/sac/agents/<name>`, `/v1/sac/agents/<name>/`,
`/v1/sac/agents/<name>/inbox/stream`, `/v1/sac/agents/<name>/_active`,
plus the fleet root `/.well-known/agent.json` (v1 renamed the
well-known file to `agent-card.json`) — fail A2A v1.0 compliance.

The AgentCard projection (`_card.py`) carries v0.x shapes too: top-level
`url` + `preferredTransport`, `defaultInputModes` /`defaultOutputModes`
spellings that A2A v1 may have reshuffled into a per-interface block,
and no `supportedInterfaces[]` array.

Backward compatibility is **not** maintained — sac has no external
consumers yet, and the simpler the v1 surface, the better the Clew
arXiv positioning ("sac is A2A v1.0 compliant", not "sac speaks a
custom dialect plus v1.0 in parallel").

## Decision

### D10. Route prefix `/v1/sac/agents/` → `/agents/`.

All sac REST routes drop the `/v1/` prefix:

| Old | New |
|---|---|
| `GET /.well-known/agent.json` | `GET /.well-known/agent-card.json` |
| `GET /v1/sac/agents/` | `GET /agents/` |
| `GET /v1/sac/agents/<name>/.well-known/agent.json` | `GET /agents/<name>/.well-known/agent-card.json` |
| `POST /v1/sac/agents/<name>` | `POST /agents/<name>/message:send` (A2A v1 REST binding) |
| `GET /v1/sac/agents/<name>/inbox/stream` | `GET /agents/<name>/inbox/stream` (sac extension) |
| `GET /v1/sac/agents/<name>/_active` | `GET /agents/<name>/_active` (sac extension) |

The `/agents/<name>/` prefix is sac's multi-agent extension above the
A2A REST binding (the spec defines single-agent paths; sac fans
several agents from one listen process).

### D11. AgentCard publishes A2A v1.0 fields only.

Verified against the lf/a2a/v1 proto (`~/proj/A2A`), not the auditor's
summary — the auditor had two wrong field names:

- Top-level `url` is **removed**. The v1 proto has no top-level `url`;
  binding URLs live under `supportedInterfaces[]` only.
- `supportedInterfaces[]` is REQUIRED. Each interface has:
  - `url` (HTTPS in prod)
  - `protocolBinding`: `"JSONRPC"` | `"GRPC"` | `"HTTP+JSON"` (the
    canonical strings — **not** `"REST"` as the auditor suggested).
  - `tenant` (sac uses the agent name as tenant)
  - `protocolVersion`: `"1.0"`
- `capabilities` carries `streaming`, `pushNotifications`,
  `extensions[]`, `extendedAgentCard` — no `stateTransitionHistory`
  (v0.x field, gone in v1).
- No top-level `authentication.schemes` (v0.x). v1 uses
  `securitySchemes` (map) + `securityRequirements[]`. Current sac
  cards advertise no auth, so we simply omit both fields rather
  than emit empty placeholders.
- `kind` discriminator: not present in current sac card, so no action.
- Enums (where sac surfaces any) follow SCREAMING_SNAKE_CASE per
  A2A v1.0.

### D12. The `notifications/claude/channel` push primitive stays sac's, not A2A.

A2A v1.0 has `tasks/pushNotificationConfig/*` for *task-level*
push, which is orthogonal to Claude Code's in-session channel. The
sac MCP push channel (ADR's commit 1–4 from today) is a Claude Code
construct, not an A2A construct — keep it where it lives, in
`server:sac` MCP, not on the A2A wire.

### D13. No backward compatibility.

Old `/v1/sac/...` paths are deleted. Tests that hit them are
updated. The change is internal to sac (no external API consumers
yet); any operator scripts that hardcoded the old paths must update
to the new ones.

## Implementation

| Layer | Where | Status |
|---|---|---|
| Route refactor (delete /v1/sac/, add /agents/) | `a2a/_server.py` | ⏳ this PR |
| AgentCard shape update (supportedInterfaces, drop top-level url, streaming=true) | `a2a/_card.py` | ⏳ this PR |
| Card consumer updates (sac mcp channel SSE URL, sidecar references) | `_mcp/channel.py`, `runtimes/_apptainer_runtime.py`, `_runners/...` | ⏳ this PR |
| Tests updated to new paths + shape | `tests/.../a2a/*`, `tests/.../runtimes/*` | ⏳ this PR |
| docs (`isolation.md`, `sac-and-orochi.md`, `spec-reference.md`) | docs | ⏳ this PR |
| A2A TCK as CI gate | `.github/workflows/a2a-tck.yml` | ⏳ follow-up |
| JWS-signed AgentCard | `a2a/_card.py` + new dep | ⏳ follow-up (ADR-0005) |

## Rationale

- **Future-proof against v1.1+**: standard paths + `supportedInterfaces[]`
  is the v1.0 shape; minor versions extend, don't break.
- **TCK passable**: the A2A Technology Compatibility Kit
  (`a2aproject/a2a-tck`) runs against the standard REST binding.
  Once routes match, `--category mandatory` becomes a CI gate.
- **Inspector friendly**: `ghcr.io/a2aproject/a2a-inspector`
  expects the standard surface; refactoring lets us point it at
  sac's localhost and get a real diff vs zero diff (= compliant).
- **Clew positioning**: "sac is A2A v1.0 compliant" is a one-line
  claim in the Methods section. The earlier alternative ("sac
  speaks A2A-like REST") was honest but weaker.

## Consequences

**Positive.**
- One step closer to upstream-mergeable example for the A2A community.
- All A2A v1.0 framework consumers (Google ADK, LangGraph, CrewAI,
  LlamaIndex, Microsoft Agent Framework) can speak to sac agents
  natively over the standard REST binding.
- TCK gate possible; passes prove sac compliance mechanically.

**Negative.**
- One-shot refactor across `_server.py`, `_card.py`,
  `_mcp/channel.py`, sidecar bind logic, and all tests. No
  parallel-path softening; the merge is the migration.
- Any operator who curl-tested the old `/v1/sac/...` paths needs to
  re-learn the new ones. Mitigation: docs updated in the same PR.

## References

- A2A v1.0 spec: <https://a2a-protocol.org/latest/specification/>
- A2A Inspector: <https://github.com/a2aproject/a2a-inspector>
- A2A TCK: <https://github.com/a2aproject/a2a-tck>
- Peer discussion that motivated this ADR — `2026-05-14` transcript.
