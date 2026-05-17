# ADR-0004: Adopt A2A v1.0 (drop `/v1/` REST prefix, v1 AgentCard shape) (2026-05-14)

**Status:** Accepted.
**Supersedes:** Earlier `/v1/sac/...` REST surface + v0.3-shaped AgentCard.
**Related:** [`0001`](0001-isolation-hardening.md), [`0003`](0003-runtime-home-directory.md).

## Problem

A2A reached v1.0 stable (Linux Foundation, April 2026) and the REST
binding now **prohibits** any path that starts with `/v1/`. sac's
existing routes — `/agents/<name>`, `/agents/<name>/`,
`/agents/<name>/inbox/stream`, `/agents/<name>/_active`,
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

### D10. Route prefix `/agents/` → `/agents/`.

All sac REST routes drop the `/v1/` prefix:

| Old | New |
|---|---|
| `GET /.well-known/agent.json` | `GET /.well-known/agent-card.json` |
| `GET /agents/` | `GET /agents/` |
| `GET /agents/<name>/.well-known/agent.json` | `GET /agents/<name>/.well-known/agent-card.json` |
| `POST /agents/<name>` | `POST /agents/<name>/message:send` (A2A v1 REST binding) |
| `GET /agents/<name>/inbox/stream` | `GET /agents/<name>/inbox/stream` (sac extension) |
| `GET /agents/<name>/_active` | `GET /agents/<name>/_active` (sac extension) |

The `/agents/<name>/` prefix is sac's multi-agent extension above the
A2A REST binding (the spec defines single-agent paths; sac fans
several agents from one listen process).

## sac extension namespace

A2A v1.0 reserves the AgentCard top level for spec-defined fields and
funnels vendor data into a namespaced extension block. **sac uses
exactly one namespace key: `x-scitex-agent-container`.** Every
sac-specific datum (role, scheduling, runtime, isolation, fleet
listings, proxy metadata) lives under that key. This contract is what
makes sac-served cards forward-compatible with vendor-neutral A2A
clients — a strict v1 validator (e.g. proto `ParseDict(AgentCard)`)
ignores the `x-*` namespace; sac-aware clients walk into it.

**Implementation source of truth:**
- `src/scitex_agent_container/a2a/_card.py::project_card` — per-agent
  card.
- `src/scitex_agent_container/a2a/_card.py::fleet_card` — fleet-level
  card.
- `src/scitex_agent_container/a2a/_card.py::validate_card_v1` — strips
  `x-*` before proto validation, so the namespace is honoured by the
  schema gate.
- `src/scitex_agent_container/_runners/a2a_proxy.py::splice_card` —
  AgentProxy runner overlay (kind / upstream / trust).

**Rules (hard).**

1. sac-specific data MUST live under `x-scitex-agent-container.*`.
   Never at the top level of the card. Never under a different
   namespace (e.g. `x-orochi`, `x-sac`, `vendor` — all forbidden).
2. `x-orochi` is owned by the orochi fleet hub, not by sac. A
   standalone `sac a2a serve` agent intentionally carries no
   `x-orochi` block.
3. `validate_card_v1` strips the `x-scitex-agent-container` key (and
   any other `x-*` key) before proto validation. New fields therefore
   only need to be valid JSON; they don't need a proto schema. This is
   the explicit forward-compat lever.

### Per-agent card fields (`project_card`)

Every per-agent card served at
`GET /agents/<name>/.well-known/agent-card.json` carries:

| Field | Source | Description |
|---|---|---|
| `x-scitex-agent-container.role_class` | `metadata.labels.role` | Operator-declared role taxonomy (e.g. `worker-telegrammer`, `head`). Mirrors `skills[0].name`. |
| `x-scitex-agent-container.cardinality` | `metadata.labels.cardinality` | `singleton` / `multi-instance` hint for fleet schedulers. |
| `x-scitex-agent-container.scheduling` | derived from `spec.host` / `spec.hosts` | `{mode: "singleton", priority: [...]}` or `{mode: "multi-instance", hosts: [...]}`. The fleet hub reads this to place the agent. |
| `x-scitex-agent-container.runtime` | `spec.runtime` | Runtime kind (`claude-code`, `agent-proxy`, etc.). |
| `x-scitex-agent-container.model` | `spec.claude.model` (v3) ∨ `spec.model` (v2 back-compat) | LLM model identifier. |
| `x-scitex-agent-container.multiplexer` | `spec.multiplexer` | tmux / zellij / none — operational tool selection. |
| `x-scitex-agent-container.required_skills` | `metadata.labels.skills` (CSV) ∪ legacy `spec.skills.required` | Skill IDs the agent loads at boot. Also merged into `skills[0].tags`. |
| `x-scitex-agent-container.isolation` | derived from `spec.apptainer.*` | Structured D3 isolation block — see below. |

The `isolation` block (see ADR-0001) carries:

| Sub-field | Type | Description |
|---|---|---|
| `level` | enum | `hardened` (default) / `relaxed` (escape hatch) / `custom` (any preflight allow-list). |
| `containall` | bool | `--containall` flag is/will be passed to apptainer. |
| `cleanenv` | bool | `--cleanenv` flag is/will be passed to apptainer. |
| `writable_tmpfs` | bool | `--writable-tmpfs` will be passed (auto-added when not relaxed and no overlay). |
| `preflight_passed` | list[str] | Preflight checks that ran and succeeded (`uid-nonzero`, `no-host-home`). |
| `preflight_allowed` | list[str] | Preflight checks the operator explicitly bypassed. |
| `binds_count` | int | Total apptainer `--bind` entries. |
| `binds_writable_count` | int | Number of binds NOT carrying `:ro`. |

External attestation surfaces (Clew, orochi) read these booleans to
prove isolation properties without re-parsing the YAML.

### Per-agent `capabilities.extensions[]` entries

Distinct from the `x-scitex-agent-container.*` namespace, the A2A v1
spec-defined `capabilities.extensions[]` array advertises sac
extensions by URI:

| URI | Emitted when | Purpose |
|---|---|---|
| `https://scitex.ai/a2a/extensions/sac-push-channel/v1` | `spec.claude.channels` contains `server:sac` | Advertises the in-session MCP push: `sac mcp channel` SSE-subscribes to `/agents/<name>/inbox/stream` and forwards events as `notifications/claude/channel` to the agent's Claude session. `params.sse_path` + `params.mcp_tools` enumerate the wire details. |

### Fleet card fields (`fleet_card`)

The fleet card at `GET /.well-known/agent-card.json` carries:

| Field | Type | Description |
|---|---|---|
| `x-scitex-agent-container.agents` | list[object] | Member directory. Each entry has `name` + `supportedInterfaces[]` (v1 shape — no top-level `url`). Spec-aware clients walk this array to fetch each member's `/agents/<name>/.well-known/agent-card.json`. |

The fleet card also advertises `capabilities.extensions[]`:

| URI | Purpose |
|---|---|
| `https://scitex.ai/a2a/extensions/sac-fleet/v1` | Declares the multi-agent directory shape. `params.members_path` and `params.member_card_path` tell vendor-neutral clients how to walk the fleet. |

### AgentProxy overlay (`splice_card`)

When a `kind: AgentProxy` agent serves its card, three additional
fields appear under `x-scitex-agent-container`:

| Field | Source | Description |
|---|---|---|
| `x-scitex-agent-container.kind` | runner constant | `"AgentProxy"` — distinguishes a proxy from a native sac runtime. |
| `x-scitex-agent-container.upstream` | `--upstream` CLI arg | Upstream A2A URL the proxy forwards to. |
| `x-scitex-agent-container.trust` | `--trust` CLI arg | Trust tier (`trusted` / `untrusted`). |
| `x-scitex-agent-container.upstream_card_fetch_error` | runtime | Present only when boot-time fetch of the upstream card failed; surfaces the error so operators see why upstream skills aren't propagating. |

See `src/scitex_agent_container/_runners/a2a_proxy.py::splice_card`.

### Cross-references

- Skill doc: [`07_a2a-protocol.md`](../../src/scitex_agent_container/_skills/scitex-agent-container/07_a2a-protocol.md)
  mirrors this enumeration for operators reading skill docs.
- Per-agent projection: `a2a/_card.py:project_card`
- Fleet projection: `a2a/_card.py:fleet_card`
- v1 schema gate: `a2a/_card.py:validate_card_v1`
- AgentProxy overlay: `_runners/a2a_proxy.py:splice_card`

## Decision (continued)

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
