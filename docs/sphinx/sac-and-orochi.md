# sac and orochi

`scitex-agent-container` (`sac`) and
[`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi) are two
separate packages with a **one-way dependency**: orochi reads from sac; sac
never imports orochi.

This doc supersedes the older "sac = one host; orochi = across hosts"
framing — sac already places and drives agents on remote hosts via SSH
hops, so it must also carry their messages. The corrected boundary is
captured below and tracked under
[`docs/adr/0008-sac-node-transport-boundary.md`](adr/0008-sac-node-transport-boundary.md).

## The comms model — what sac knows

sac sees a single thing on its comms graph: **nodes**. A node is an
*identity* + an *inbox* + an *ACL*. There are two kinds, distinguished
*only* by who owns the lifecycle:

| Kind          | Lifecycle owner                                       | Example                                                                       |
|---------------|-------------------------------------------------------|-------------------------------------------------------------------------------|
| sac-managed   | sac (spec, container, start/stop/health)              | An agent declared in a `spec.yaml`, launched by `sac agent start`.            |
| **external**  | NOT sac — the operator, a separate tool, a human      | A plain `claude` CLI session, joining via `sac mcp channel --name <id>`.      |

Both kinds are equal on the comms graph. Whatever *role* the orchestration
layer assigns a node — "lead", "head", "worker", "coordinator" — is
irrelevant to sac; sac sees only "a node".

### Lineage — parent / children

sac records who called `sac agents start`: the caller is the **parent**,
the spawned agent its **child**. A parent may have many children; every
node is a parent or a child (the root is a parent with no parent of its
own).

### Group — the unit of default ACL

A parent together with its direct children is one **group**. The group is
the unit of intra-group default ACL: within a group every node may
message every other, **bidirectionally** (parent↔child *and*
sibling↔sibling).

### Depth limit — a POLICY, not a structural limit

We currently forbid a child from spawning children, so the *live*
hierarchy is two levels (root + its direct children). **This is a
policy constraint we choose to enforce, not an architectural ceiling.**
The model, schema, lineage and group logic remain **N-level capable** —
recursion is the natural shape; nothing hard-codes "2" or assumes a
fixed depth. The cap is a *lift-able policy* (a rule that denies a
non-root spawn). Lifting it later is a policy change only — zero
structural / schema / data-structure change.

### ACL — permissioned messaging

- Intra-group is allowed by default (bidirectional).
- Cross-group requires an **explicit ACL grant** (per-node config:
  `spec.comms.allow` for sac-managed; a policy store for external nodes).
- The graph is permissioned — never implicitly all-to-all. An ungated
  channel is a prompt-injection vector
  ([Claude Code channels reference](https://code.claude.com/docs/en/channels-reference)).

> ⚠️ **Identity caveat — ACL is policy-shaped today** (handoff
> 2026-05-20 limited-scope decision).
>
> The shipped ACL gates on the **self-claimed
> `metadata.from_agent`** field; cryptographic per-node identity
> is **deferred** to a separate follow-on handoff. The design for
> the cryptographic-identity work is filed in scitex-lead at
> `GITIGNORED/FUTURE/sac-per-node-authenticated-acl.md` — not in
> this repo. Every cross-group grant row in `comms_grants`
> carries the audit note
> `"trusts metadata.from_agent until per-node creds land"` so the
> follow-on work has the audit trail it needs to scope what each
> grant must be re-validated against.
>
> Until that lands, the ACL is a policy gate — a misbehaving node
> can claim a different identity by writing a different
> `from_agent`. The handoff acceptance "identity cannot be
> spoofed via a metadata field" will be met by the follow-on, not
> by the limited-scope WI-2.

### A2A compliance

Every node, sac-managed or external, is addressable via the A2A protocol;
a node's identity *is* its A2A AgentCard.

- sac-managed nodes derive their AgentCard from `spec.yaml` (see
  [`a2a/_card.py::project_card`](https://github.com/ywatanabe1989/scitex-agent-container/blob/develop/src/scitex_agent_container/a2a/_card.py)).
- External nodes have no YAML, so sac **synthesises a minimal AgentCard
  at registration** when `sac mcp channel --name <id>` connects: identity
  + inbox endpoint + the required A2A capability fields, and *nothing*
  runtime/container-shaped. The synthesised card is the external node's
  whole definition (see
  [`_listen/_nodes.py::synthesize_external_card`](https://github.com/ywatanabe1989/scitex-agent-container/blob/develop/src/scitex_agent_container/_listen/_nodes.py)).
  The card carries `"x-scitex-agent-container.node_kind": "external"` so
  downstream tooling can distinguish the kinds.

### What sac knows vs doesn't

| sac knows                                | sac does NOT know                       |
|------------------------------------------|-----------------------------------------|
| Nodes (sac-managed + external)           | Roles ("lead", "head", "worker", …)     |
| Lineage (parent / children)              | Topology visualisations                 |
| Groups (parent + direct children)        | Connectivity mesh (cloudflared, autossh) |
| ACL grants                               | Human chatops UI                        |

## The corrected boundary

```
            ┌────────────────────┐                       ┌──────────────────────┐
            │   Human operator   │  chat · DM · channel  │ claude-code-         │
            │   (web UI / CLI)   │ ◄───── alerts ─────── │ telegrammer          │
            └─────────┬──────────┘                       │ Telegram MCP + TUI   │
                      │                                  └──────────▲───────────┘
                      ▼                                             │
        ┌──────────────────────────────────┐                        │
        │   scitex-orochi                  │                        │
        │   web chatops UI · dashboard     │                        │
        │   topology / presence            │                        │
        │   channels · DMs · threads       │                        │
        │   connectivity mesh              │                        │
        │   (cloudflared / autossh)        │                        │
        └─────────────────┬────────────────┘                        │
                          │  CONSUMES sac transport                 │
                          │  (one-way dep: orochi → sac)            │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   scitex-agent-container (sac)   │                        │
        │   NODE TRANSPORT SUBSTRATE       │                        │
        │     any-node ↔ any-node          │                        │
        │     any host (same / LAN /       │                        │
        │     SSH-alias / tunnel)          │                        │
        │     ACL-gated                    │                        │
        │   Lifecycle for sac-managed      │                        │
        │   nodes only (apptainer)         │                        │
        │   Zero knowledge of orochi       │                        │
        └─────────────────┬────────────────┘                        │
                          │  starts / supervises (sac-managed)      │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   Claude agents + external nodes │ ── heartbeat-push ──▶ orochi
        │   (sac-managed)   (external)     │ ── alerts ─────────────┘
        └──────────────────────────────────┘
```

### Responsibility split

| Concern                                                                   | Owner       |
|---------------------------------------------------------------------------|-------------|
| Agent process (SDK + session.jsonl)                                       | **sac**     |
| Per-host control plane (start/stop/send/tail/list)                        | **sac**     |
| Container runtime (apptainer)                                             | **sac**     |
| **Node transport (any-to-any, any host, ACL-gated)**                      | **sac**     |
| **Channel-event durability + replay** (`channel_events` in state.db)      | **sac**     |
| **External-node inbox** (no YAML, identity + inbox only)                  | **sac**     |
| In-session push (MCP channel server `server:sac`)                         | **sac**     |
| Human chatops UI (Slack-like web interface)                               | **orochi**  |
| Topology / presence / dashboard                                           | **orochi**  |
| Channels / DMs / threads as features                                      | **orochi**  |
| Connectivity mesh (cloudflared + autossh) — *establishes* host reachability | **orochi**  |

**Rule:** sac is the node-transport substrate. Orochi is the human/product
layer that *consumes* sac transport. The connectivity mesh that
establishes reachability lives in orochi; sac assumes reachability.

## How orochi consumes sac

orochi reads sac's state files
(`~/.scitex/agent-container/runtime/<name>/heartbeat.json`,
`session.jsonl`, `state.db`) from each host it manages. It also calls
sac's HTTP endpoints (`/agents/<name>/{message:send,inbox/stream}`) like
any other A2A v1 client. It never calls `sac` CLI commands directly —
the contract is the on-disk state files plus the published HTTP
endpoints.

Agents push heartbeats and alerts to orochi via the `server:orochi-push`
MCP channel, configured in `spec.claude.channels`:

```yaml
spec:
  claude:
    channels:
      - server:orochi-push
```

The `server:sac` channel is sac's own primitive — agents enable it the
same way:

```yaml
spec:
  claude:
    channels:
      - server:sac
```

When `server:sac` is in `channels`, `sac agent start` auto-spawns
`sac mcp channel --name <agent>` as a stdio MCP subprocess of the agent's
Claude session; it subscribes to the host-local
`/agents/<name>/inbox/stream` SSE and pushes
`notifications/claude/channel` so the agent sees
`<channel source="sac" ...>` tags in real time. External nodes opt in by
running the same command from their own Claude session — no container,
no spec.

## Standalone use

sac works fully without orochi. If you don't need cross-host chatops,
just omit `server:orochi-push` from `spec.claude.channels` and skip the
orochi install entirely. The sac comms graph still works: nodes message
each other, the channel bus is durable, and the ACL still gates.

## Live instance

[https://scitex-orochi.com](https://scitex-orochi.com)
