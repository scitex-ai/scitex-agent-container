---
description: |
  [TOPIC] A2A AgentCard `x-scitex-agent-container.*` extension fields
  [DETAILS] Per-agent / fleet / proxy field enumeration + concrete JSON example for the sac namespace projected into A2A v1 AgentCards.
tags: [scitex-agent-container-a2a-protocol-extension-fields]
---

# A2A AgentCard — `x-scitex-agent-container.*` extension fields

Companion to [`07_a2a-protocol.md`](07_a2a-protocol.md). That leaf documents the protocol surface (CLI, auto-launch, handlers); this leaf enumerates every vendor field projected under the sac namespace and shows one full card.

A2A v1.0 reserves the AgentCard top level for spec-defined fields and funnels vendor data into a namespaced extension block. **sac uses exactly one namespace key: `x-scitex-agent-container`.** Every sac-specific datum lives under that key, never at the top level, never under another vendor namespace (another project's extension namespace is owned by that project, not by sac). See [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md).

## v3 YAML — what gets projected

| Field | Mapped to AgentCard |
| --- | --- |
| parent-directory stem (dir-as-SSoT) | `name` — v3 derives the agent name from the YAML's parent dir; the legacy `metadata.name` field is rejected by `_validation.py` |
| `metadata.labels.capabilities` (CSV) | first item → `description`; all items → `skills[0].tags` |
| `metadata.labels.team` | `provider.organization` |
| `metadata.labels.role` | `skills[0].name`, `x-scitex-agent-container.role_class` |
| `metadata.labels.function` (CSV) | `skills[0].description` |
| `metadata.labels.skills` (CSV) | `skills[0].tags` ∪ `x-scitex-agent-container.required_skills` |
| `spec.host` / `spec.hosts` | `x-scitex-agent-container.scheduling` |
| `spec.runtime` / `claude.model` / `multiplexer` | `x-scitex-agent-container.runtime` / `.model` / `.multiplexer` |
| `spec.apptainer.*` | `x-scitex-agent-container.isolation.*` (D3 attestation block) |
| `spec.claude.channels: [server:sac]` | `capabilities.extensions[]` (sac-push-channel/v1) |

## Per-agent card fields

Emitted by `a2a/_card.py::project_card`. Every per-agent card served at `GET /agents/<name>/.well-known/agent-card.json` carries:

| Field | Source | Description |
| --- | --- | --- |
| `x-scitex-agent-container.role_class` | `metadata.labels.role` | Operator-declared role taxonomy (e.g. `worker-telegrammer`). Mirrors `skills[0].name`. |
| `x-scitex-agent-container.cardinality` | `metadata.labels.cardinality` | `singleton` / `multi-instance` hint for fleet schedulers. |
| `x-scitex-agent-container.scheduling` | `spec.host` / `spec.hosts` | `{mode, priority|hosts}` placement hint. |
| `x-scitex-agent-container.runtime` | `spec.runtime` | Runtime kind (`claude-code`, `agent-proxy`, etc.). |
| `x-scitex-agent-container.model` | `spec.claude.model` ∨ `spec.model` (legacy) | LLM model identifier. |
| `x-scitex-agent-container.multiplexer` | `spec.multiplexer` | tmux / zellij / none. |
| `x-scitex-agent-container.required_skills` | `metadata.labels.skills` (CSV) — `spec.skills` is rejected in v3; skills now live as files under `to_home/.claude/skills/` | Skill IDs the agent loads at boot. |
| `x-scitex-agent-container.isolation` | derived from `spec.apptainer.*` | D3 attestation block — `{level, containall, cleanenv, writable_tmpfs, preflight_passed, preflight_allowed, binds_count, binds_writable_count}`. External attestation surfaces (Clew, fleet hubs) read these booleans. |

## Per-agent `capabilities.extensions[]` entries

The A2A v1 spec-defined `capabilities.extensions[]` array advertises sac extensions by URI (distinct from `x-scitex-agent-container`):

| URI | Emitted when | Purpose |
| --- | --- | --- |
| `https://scitex.ai/a2a/extensions/sac-push-channel/v1` | `spec.claude.channels` contains `server:sac` | In-session MCP push: `sac mcp channel` SSE-subscribes to `/agents/<name>/inbox/stream` and forwards events as `notifications/claude/channel` to the agent's Claude session. `params.sse_path` + `params.mcp_tools` enumerate wire details. |

## Fleet card fields

Emitted by `a2a/_card.py::fleet_card` at `GET /.well-known/agent-card.json`:

| Field | Type | Description |
| --- | --- | --- |
| `x-scitex-agent-container.agents` | list[object] | Member directory. Each entry has `name` + `supportedInterfaces[]`. Spec-aware clients walk this array to fetch each member's per-agent card. |

Plus the fleet-level `capabilities.extensions[]`:

| URI | Purpose |
| --- | --- |
| `https://scitex.ai/a2a/extensions/sac-fleet/v1` | Declares the multi-agent directory shape. `params.members_path` + `params.member_card_path` tell vendor-neutral clients how to walk the fleet. |

## AgentProxy overlay (`kind: AgentProxy`)

Emitted by `_runners/a2a_proxy.py::splice_card` when an AgentProxy runner serves its card:

| Field | Source | Description |
| --- | --- | --- |
| `x-scitex-agent-container.kind` | runner constant | `"AgentProxy"` — distinguishes a proxy from a native sac runtime. |
| `x-scitex-agent-container.upstream` | `--upstream` CLI arg | Upstream A2A URL the proxy forwards to. |
| `x-scitex-agent-container.trust` | `--trust` CLI arg | Trust tier (`trusted` / `untrusted`). |
| `x-scitex-agent-container.upstream_card_fetch_error` | runtime | Present only when boot-time fetch of the upstream card failed. |

## Concrete example

Per-agent card served at `GET /agents/my-agent/.well-known/agent-card.json`:

```json
{
  "name": "my-agent",
  "description": "sac agent: my-agent (worker-telegrammer)",
  "version": "scitex-agent-container/v3",
  "supportedInterfaces": [
    {
      "url": "http://127.0.0.1:8888/agents/my-agent",
      "protocolBinding": "HTTP+JSON",
      "tenant": "my-agent",
      "protocolVersion": "1.0"
    }
  ],
  "provider": {"organization": "scitex-agent-container", "url": "https://scitex.ai"},
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "extendedAgentCard": false,
    "extensions": [
      {
        "uri": "https://scitex.ai/a2a/extensions/sac-push-channel/v1",
        "description": "In-session MCP push: sac mcp channel subscribes ...",
        "required": false,
        "params": {
          "sse_path": "/agents/my-agent/inbox/stream",
          "mcp_tools": ["a2a_send", "a2a_reply", "a2a_ack", "a2a_peers", "a2a_inbox"]
        }
      }
    ]
  },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {
      "id": "my-agent.worker-telegrammer",
      "name": "worker-telegrammer",
      "description": "relays telegram messages",
      "tags": ["a2a", "telegram"]
    }
  ],
  "x-scitex-agent-container": {
    "role_class": "worker-telegrammer",
    "cardinality": "singleton",
    "scheduling": {"mode": "singleton", "priority": ["ywata-note-win"]},
    "runtime": "claude-code",
    "model": "claude-opus-4-7",
    "multiplexer": "tmux",
    "required_skills": ["quality-guards", "autonomous", "speech", "scitex"],
    "isolation": {
      "level": "hardened",
      "containall": true,
      "cleanenv": true,
      "writable_tmpfs": true,
      "preflight_passed": ["uid-nonzero", "no-host-home"],
      "preflight_allowed": [],
      "binds_count": 3,
      "binds_writable_count": 1
    }
  }
}
```

## Cross-references

- [ADR-0004](../../../../docs/adr/0004-a2a-v1-compliance.md) — A2A v1.0 compliance + the authoritative `x-scitex-agent-container.*` field enumeration this doc mirrors
- Implementation source of truth: `a2a/_card.py::project_card`, `a2a/_card.py::fleet_card`, `_runners/a2a_proxy.py::splice_card`
