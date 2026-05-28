# ADR-0014 — Symmetric federated comms graph (cross-host A2A registry)

## Status

Proposed (2026-05-28).

## Context

Cross-host A2A is currently one-directional. The forward direction
(lead → spartan-agent via ``ssh-transport``) works because the lead's
``state.db.instances`` table records the spartan-agent's host + port,
which ``_state/state_db_nodes.resolve_node_host`` looks up. The reverse
direction (spartan-agent → lead) fails at peer resolution: the spartan
host's ``state.db`` has no ``instances`` row for ``lead``, so
``resolve_node_host`` returns ``None`` and the listen server treats
``lead`` as a local-only node — which it is not.

The forward path inside ``_listen/_node_channel._forward_to_remote`` is
already correct and complete. The only blocker is that the receiving
host has no way to know that ``lead`` lives on another host. Every
existing primitive needed to fix this is already in the codebase:

- ``_state/state_db_export.export_state`` / ``import_state`` — JSON
  delta format, idempotent ``INSERT OR IGNORE`` on PK, ``--since``
  filter.
- ``_state/host_config.py`` — ``peers:`` block + globs (e.g.
  ``spartan-*``) + ``ssh_control_options`` for ControlMaster reuse +
  ``LeadConfig``.
- ``_listen/peer_tokens.py`` — ``peer-tokens/<host>.token`` bearer
  registry, provisioned by ``sac host add-peer``.
- ``_listen/_node_channel._forward_to_remote`` — does the actual
  cross-host forward once a destination is known.

## Decision

Land a **symmetric federated** ``comms_nodes`` table that every host
holds and every host writes into. No lead-privileged hub. Reconciliation
is ssh-pull anti-entropy reusing ``sac db export/import`` — proven
Consul/etcd shape, no new transport, no signing layer (operator-managed
ssh trust + per-host bearer tokens remain the trust boundary).

Five components, staged so this PR (Stage 1) does only the federation
backbone:

### 1. ``comms_nodes`` table

```
CREATE TABLE IF NOT EXISTS comms_nodes (
    name TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    a2a_port INTEGER NOT NULL,
    registered_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    source_host TEXT,
    ended_at REAL
);
```

``name`` is globally unique. ``source_host = NULL`` means the row was
written locally (operator identity or agent-start hook on this host);
non-NULL means the row was synced from another host's export. ``ended_at``
is a soft tombstone — preserved on ``unregister`` so the JSON delta
carries the deletion to peers, then GC'd by an unrelated maintenance
pass.

Added to ``KNOWN_TABLES`` so ``sac db export/import`` handles it without
further wiring.

### 2. ``resolve_node_host`` fallback

Today: ``SELECT host, a2a_port FROM instances WHERE name = ?
AND ended_at IS NULL``. Returns ``None`` → caller treats as local.

After: if no ``instances`` row matches, fall through to a SELECT against
``comms_nodes`` (ignore tombstoned rows). Same return shape. The
existing local-fallback in ``_listen/_node_channel.py`` is preserved
— a node that is in neither table is still treated as local (used
for newly-spawned nodes that haven't been registered yet).

### 3. ssh-pull anti-entropy via ``sac registry sync``

```
sac registry sync [--from PEER] [--to PEER] [--all] [--dry-run]
```

``--from PEER`` ssh's to the peer, runs ``sac db export --tables
comms_nodes``, pipes the JSON back, imports locally. ``--to PEER`` is
the reverse. ``--all`` walks every entry in ``peers:`` (resolved
globs), pulls from each, then pushes to each — per-peer errors are
logged and do not abort the run. ``--dry-run`` skips ssh and writes.

This sub-fork chose ssh-pull (α) over an HTTP-endpoint variant (β):
ssh + ``sac db export`` is already in the codebase, requires zero new
auth, and reuses ControlMaster multiplexing.

### 4. Registration hooks

- Listen startup (``_listen/server.py`` / ``cli_pkg/listen_cmds.py``):
  on bind, write a ``comms_nodes`` row for the host's operator
  identity (e.g. ``lead`` on the lead host) sourced from ``LeadConfig``.
  Optionally trigger ``sac registry sync --all`` once, gated by a
  config flag ``comms_nodes.sync_on_start: bool = True``.

- Agent start (``cli_pkg/lifecycle/_dispatch._dispatch_remote_start``):
  paired with the existing ``record_instance_start`` write, also call
  ``register_comms_node`` so the cross-host agent is visible to peers
  after a sync. Agent stop tombstones the row.

This sub-fork chose startup-trigger + manual sync (α) over a
background-loop daemon (β): a startup propagation is enough to fix
the empirical bug, and operators can always run ``sac registry sync
--all`` manually; a tick loop is deferable.

### 5. Conflict policy: fail-loud on name collision

If two hosts independently register the same ``name`` with different
``(host, a2a_port)``, ``register_comms_node`` raises
``CommsNodeConflictError`` rather than overwriting silently. This
mirrors ADR-0011's loud-failure principle: a name collision is an
operator-config error and silently picking a winner (LWW) would hide
the misconfiguration. Same ``(name, host, a2a_port)`` is idempotent —
just bumps ``updated_at``.

This sub-fork chose fail-loud (α) over last-writer-wins (β): LWW
would let a misconfigured Spartan stomp the lead's authoritative row
on the next pull.

### Staged rollout

- **Stage 1 (this ADR / this PR):** ``comms_nodes`` table + resolver
  fallback + ``sac registry sync`` + startup hooks. Closes the
  one-directional A2A bug.

- **Stage 2 (planned, separate PR):** unify turn-mode and push-mode
  inside ``_node_channel`` so the cross-host forward no longer hits
  the 120s ``inbox/stream`` timeout when a target is a push-mode-only
  node.

- **Stage 3 (planned, separate PR):** cross-host ACL federation
  (export/import ``comms_grants``) and fix the container → host
  grant-scope bug where a grant minted inside the container isn't
  seen by the host's listen.

## Non-goals

- A lead-privileged registry hub — sac's architecture rejects single
  points of failure (see ADR-0013's rejected "central-only" path).
- A new transport — ssh + the existing ``sac db export/import`` is the
  whole anti-entropy mechanism.
- Cryptographic signing of registry deltas — the trust boundary stays
  at the per-host bearer + operator-managed ssh known_hosts.
- Gossip-style failure detection — Stage 1 does no liveness eviction;
  a stale row is preferable to a noisy false-positive eviction. (ADR-0013
  already covers heartbeat-driven liveness for the ``instances``
  registry; comms_nodes will inherit that policy later.)
- ``requires_reply`` / per-message routing schema — deferred to Stage 2.

## Trust

Cross-host pulls authenticate via ssh (operator-managed
``~/.ssh/known_hosts`` + ``ssh_control_options``); the receiving host
inserts via ``import_state``'s ``INSERT OR IGNORE``. The per-host
bearer in ``peer-tokens/<host>.token`` continues to authenticate
``_node_channel._forward_to_remote`` calls — comms_nodes only feeds
``resolve_node_host`` with a destination; the actual A2A POST still
runs the existing auth path.

## Conflict policy

Names are globally unique. Two hosts cannot both claim ``lead``;
``register_comms_node`` raises ``CommsNodeConflictError`` and the
operator must rename or unregister one. Same row + bumped
``updated_at`` is idempotent. Tombstoned rows can be re-activated by
clearing ``ended_at`` (the next sync from the originating host will
do this naturally).

## Alternatives considered

- **HTTP endpoint on ``sac listen`` for sync (β).** Rejected: requires
  new auth (the bearer is for A2A, not for fleet maintenance) and a
  new route surface; ssh + ``sac db export`` is already there.
- **Background sync daemon (β).** Rejected: Stage 1 only needs to
  close the bidirectionality bug; a tick loop is a separate concern
  that can land later without rework.
- **Last-writer-wins on name conflict (β).** Rejected: LWW hides
  operator misconfiguration; fail-loud surfaces it.
- **Central registry on lead (per ADR-0013).** ADR-0013 covers the
  *instances* registry (running state). The *comms* graph (who can
  speak to whom) is symmetric by design — every host must answer
  ``resolve_node_host(lead)`` correctly without depending on the
  lead being reachable.

## References

- ADR-0008 — sac node transport boundary.
- ADR-0010 — agent-spawn family tree + server-managed ACL.
- ADR-0011 — fail-loud config resolution (the loud-failure pattern
  used here on name collision).
- ADR-0013 — central propagating fleet registry (the *instances*
  story; this ADR is the *comms* story).
