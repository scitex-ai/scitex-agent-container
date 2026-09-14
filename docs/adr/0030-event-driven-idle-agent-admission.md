# ADR-0030 — Event-driven idle-agent admission

Status: accepted (2026-09-14)

## Context

A Hermes session heartbeat submits a normal model turn. For a long-lived
agent, that turn includes the complete conversation and can consume a shared
Qwen admission slot merely to ask whether new work exists. Pausing a persisted
heartbeat reduced this traffic, but left resumable heartbeat state and a race
between TUI attachment and the separate recovery observer.

The fleet already has a deterministic work detector. Cards `notifyd` publishes
durable notifications when assigned work needs attention, and the SAC/Cards/CCT
ingress workers deliver those envelopes to the harness. Human and A2A messages
use the same durable delivery boundary. These transports can observe work
without invoking the model.

Runtime evidence on 2026-09-14 distinguished these paths. All six compute-03
Hermes sessions reported their native heartbeat as `paused`. Recent turns in
the Hermes state store were preceded by durable user envelopes such as
`<channel source="notifyd" ...> STALE-ACTIVE ...` or direct agent messages.
They were not periodic heartbeat firings. The most recent native heartbeat
timestamps preceded the current owner generations.

The same inspection found two detached recovery observers for `scitex-hub`:
one launched on 2026-09-12 from the mutable dotfiles spec and one launched on
2026-09-14 from the current authority snapshot. Both had been reparented to
PID 1. The pidfile could describe only one observer, and the old exact-spec
ownership check could not recognize the other after its config path changed.

## Decision

The SAC-owned Hermes TUI removes persisted session heartbeat state through
Hermes' `session.control` action `heartbeat.clear` as soon as the exact owned
session appears in `session.active_list`. It does this before publishing the
session as attached in SAC supervision state. A transient control failure does
not restart or stop the TUI; the owner records the failure and retries on its
next bounded observation.

The same clear operation runs before an in-place stale-provider recovery. This
prevents an old periodic turn from racing the provider rebind.

Recovery observer identity is agent-scoped across incarnations. New observers
carry an explicit `--name`; launch reconciles every exact-module process with
that name before spawning the singleton. For migration, an older observer
without `--name` is eligible only when its authored spec still resolves to the
same agent name. PID plus process creation time guards escalation against PID
reuse. A different agent or a substring name is never eligible.

Idle wakeup is event-driven:

- a durable SAC, Cards, CCT, A2A, or human message starts or steers a turn;
- Cards `notifyd` may emit a durable message after it deterministically finds
  assigned work needing attention;
- process liveness heartbeats remain telemetry and never call the model;
- an idle agent with no durable event consumes no inference slot.

## Consequences

Legacy native heartbeat state is removed on the next ordinary launch or TUI
reattachment. It cannot be resumed accidentally later. Agent sessions and
their transcript identity remain intact.

Orphaned recovery observers are retired at the next ordinary launch even when
their authored config path differs from the current incarnation. This avoids
duplicate pane polling and competing recovery actions without treating an
unproven process as SAC-owned.

`autonomous.enabled` continues to govern multi-turn work execution and recovery
monitoring. It no longer authorizes blind periodic full-context turns. Work
detection belongs to the durable transport/control plane, not model inference.

The owner supervision projection includes `periodic_turns: disabled` only
after Hermes confirms that no heartbeat state remains. This gives deployment
verification a deterministic assertion instead of relying on pane text.
