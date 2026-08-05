---
orphan: true
---

# ADR-0008: sac is the node-transport substrate; comms model is nodes / lineage / groups / ACL (2026-05-20)

**Status:** Accepted.
**Supersedes:** the implicit "sac = one host; an external hub = across
hosts" framing carried by the package docs before 2026-05-20.
**Related:** [`ADR-0004`](0004-a2a-v1-compliance.md) (A2A v1 compliance —
§D12 defines the push primitive this ADR builds on).
**Handoff:** `GITIGNORED/HANDOFF_AGENT_COMMS_2026-05-19.md`.

## Problem

Two boundaries had drifted out of alignment with reality:

1. **The "one host vs many hosts" split between sac and the fleet hub
   was wrong.** sac already places and drives agents on remote hosts via
   `spec.host` (and SSH hops in `spec.remote.hops`); the moment it does
   so, it must also carry their messages — there is no "off-host" sac
   could hand the messaging off to without a hub installed. Yet the
   doc said "sac = one host, the hub = across hosts."

2. **sac had no model for non-sac-managed nodes on its comms graph.**
   A plain `claude` CLI session — the official CLI is stable and good
   for terminal/human interaction — could not address or be addressed by
   a sac-managed agent without sac inheriting its lifecycle. That made
   "lead = plain `claude` CLI, workers = sac-managed agents" impossible
   to express, even though the operator regularly runs exactly that
   topology.

The remediation is a single ADR because the two are intertwined: the
non-sac-managed-node concept *only* makes sense once sac is the
transport substrate (not the lifecycle manager that happens to also
forward messages).

## Decision

### D1. sac is the node-transport substrate

sac carries messages **any node ↔ any node**, on **any host** (same
host, LAN, SSH-alias, tunnel), **ACL-gated**, self-sufficient with no
fleet hub installed.

The hub is the human/product layer: web chatops UI, dashboard, topology
viz, presence, channels/DMs/threads as features, *and* the connectivity
mesh (cloudflared / autossh) that **establishes** host reachability.
The hub is a *consumer* of sac transport, not the transport.

sac assumes reachability. The hub establishes it.

### D2. Two kinds of node, distinguished only by who owns the lifecycle

A **node** is an *identity* + an *inbox* + an *ACL*. Two kinds:

* **sac-managed** — sac owns the lifecycle (spec, container,
  start/stop/health). Lifecycle entry point: `sac agent start`.
* **external** — sac does **not** own the lifecycle. Typically a plain
  `claude` CLI session. Joins the comms graph by running
  `sac mcp channel --name <id> --listen-url <sac listen>`. No
  container. No spec. sac never starts or stops an external node.

Both kinds are equal on the comms graph. The orchestration layer may
assign a role ("lead", "worker", "coordinator") to a node — sac sees
only "a node".

### D3. Lineage and groups

sac records who called `sac agents start`: the caller is the **parent**,
the spawned agent its **child**. A parent together with its direct
children is one **group**. The group is the unit of default ACL.

### D4. ACL — permissioned messaging

* Intra-group sends are allowed by default (parent↔child *and*
  sibling↔sibling, bidirectional).
* Cross-group sends require an **explicit ACL grant**
  (`spec.comms.allow` for sac-managed nodes; a policy store for
  external nodes).
* The graph is permissioned — never implicitly all-to-all. An ungated
  channel is a prompt-injection vector
  ([Claude Code channels reference](https://code.claude.com/docs/en/channels-reference)).

### D5. Depth limit is a POLICY, not a structural ceiling

We currently forbid a child from spawning children, so the *live*
hierarchy is two levels. The model, schema, lineage and group logic
remain **N-level capable** — recursion is the natural shape; nothing
hard-codes "2" or assumes fixed depth. The cap is a lift-able policy
rule. Lifting it later is a policy change only — zero
structural / schema / data-structure change.

### D6. A2A compliance for both kinds of node

Every node, sac-managed or external, is addressable via A2A; a node's
identity *is* its A2A AgentCard.

* sac-managed: the card is projected from `spec.yaml` by
  `a2a/_card.py::project_card`.
* External: the card is **synthesised at registration** by
  `_listen/_nodes.py::synthesize_external_card` — identity + inbox
  endpoint + the v1-required capability fields, marked
  `"x-scitex-agent-container.node_kind": "external"`. Registration is
  implicit: the first `message:send` / `inbox/stream` touch on a name
  mints + caches the card.

### D7. Durability of the channel bus

Every channel event is persisted to `state.db.channel_events` before
fan-out. The SSE handler replays missed events on connect
(`Last-Event-ID` cursor) and stamps each frame with `id: <row_id>`,
marking the row delivered as it ships. An event POSTed while no
subscriber is connected is delivered on the next connect; a
kill + reconnect replays exactly the missed events; nothing is dropped
silently.

Schema: `(id, target, source, kind, content, meta_json, ts, delivered_at)`.

### D8. Endpoint location for the inbox surface

The inbox endpoints (`message:send` and `inbox/stream`) live on the
always-on `sac listen` host control-plane (`_listen/server.py`),
**keyed by node identity** — they accept a name that has no YAML and
no container. The per-agent `a2a/_server.py` is a separate surface
for sac-managed-agent SDK plumbing.

### D9. The transport is Claude Code's official channels feature

No new channel protocol. The MCP server emits
`notifications/claude/channel`; Claude Code renders it as a
`<channel source="..." ...>` block in the running session. This is
the same protocol a fleet hub's own push channel uses — sac just adds
the `server:sac` flavour pointing at the local listen.

## What this rules out

* No UI, dashboard, or topology viz in sac — the hub's.
* No hub awareness in sac. The boundary stays one-way (hub → sac).
* No new channel protocol — use Claude Code channels as-is.
* No "lead" / "head" / "worker" / role concept in sac — only nodes,
  lineage, groups, ACL.
* No sac lifecycle management of external nodes — sac never starts or
  stops any external node.
* No per-agent role/address config beyond `spec.comms` ACL.

## What this opens up (deliberately, but not built yet)

The model is shaped to accept **transport adapters** in a phase-2:

* A **human adapter** (a human is a node too — reachable via Telegram /
  phone / email; replies in that app). Backed by
  `claude-code-telegrammer` + `scitex-notification` today; phase 2
  registers them behind the same `node = identity + inbox + ACL` model.
* A possible HTTP webhook adapter or a hub UI as an adapter.

The phase-1 work (WI-1..6 from the handoff) builds exactly one adapter:
the Claude-session adapter (`sac mcp channel`, MCP
`notifications/claude/channel`). The model is deliberately
adapter-shaped so a second adapter can be added later without touching
the core. The only phase-2 obligation on phase-1 is: keep the adapter
seam clean — the node model, ACL and routing must not assume the
Claude-session adapter is the only transport.

## Implementation sources of truth

* Node-identity inbox endpoints + external-node registry —
  `src/scitex_agent_container/_listen/_nodes.py`,
  `src/scitex_agent_container/_listen/server.py`.
* Channel-event durability + replay —
  `src/scitex_agent_container/_state/state_db_channel.py`,
  `channel_events` table schema in
  `src/scitex_agent_container/_state/state_db.py`.
* SSE replay handler — `a2a/_server.py::get_inbox_stream` (and the
  symmetric handler in `_listen/server.py::node_inbox_stream`).
* Card projection (sac-managed) — `a2a/_card.py::project_card`.
* Card synthesis (external) — `_listen/_nodes.py::synthesize_external_card`.

## References

* `GITIGNORED/HANDOFF_AGENT_COMMS_2026-05-19.md` — the handoff that
  motivated this work; superseded once WI-1..6 land.
* [Claude Code channels reference](https://code.claude.com/docs/en/channels-reference)
  — protocol + the "gate inbound messages" guidance that directly
  informs D4.
* `docs/design/telegram-fold.md` — the consumer flow
  (telegram → Broker → SSE → `sac mcp channel` → session) that
  prefigured this model.
