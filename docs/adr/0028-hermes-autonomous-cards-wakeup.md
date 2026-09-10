# ADR-0028 — Hermes autonomous Cards wakeup

Status: accepted (2026-09-11)

## Context

An official Hermes TUI returning to its `ready` composer after one completed
turn is healthy behavior, not a process-liveness failure. SAC previously
compiled `spec.autonomous.max_turns` into Hermes configuration but did not arm
anything that created a later turn. Consequently an autonomous application
agent could report one blocked or completed slice and remain ready forever,
even while its durable Cards board contained independent work.

Restarting the agent is not a work scheduler. A SAC polling loop that sends a
model turn every few seconds would duplicate Hermes' control plane, spend
tokens while busy, race human steering, and make Cards ownership less safe.

## Decision

For `harness: hermes`, `runtime: tui`, and `autonomous.enabled: true`, SAC sends
one Hermes-native `/heartbeat` control command after launch succeeds. Its
interval is `autonomous.idle_kick_after_s`, with the upstream Hermes minimum of
60 seconds. The heartbeat prompt includes the authored
`autonomous.kick_text` plus a fixed Cards safety contract:

- re-read the live durable board on every wake;
- continue already-owned work before looking for a new card;
- before edits, verify current assignment/ownership and file/scope overlap;
- claim at most one eligible unowned card, and never edit another agent's
  claimed scope;
- if Cards is unavailable or no safe work exists, report briefly and idle.

Hermes owns due-time polling, idle detection, coalescing, and its input queue.
Its heartbeat fires only when the run is idle and the queue is empty, which
preserves human messages, steering, and interruption. SAC neither polls Cards
nor runs a second scheduler. A refused arming command is logged loudly, once;
it is not retried in a busy loop and the live TUI remains available to a human.

Hermes goals still own multi-turn completion of one selected objective.
Hermes cron still owns durable wall-clock schedules and delivery. The session
heartbeat owns the narrower transition from “finished/blocked this slice and
idle” to “look for another safe durable Card.”

## Consequences and migration

Existing specs are unchanged because autonomy remains opt-in. Existing Hermes
TUI specs with `autonomous.enabled: true` acquire the wakeup on their next
ordinary launch; this change does not require or authorize restarting a live
agent. Values below 60 seconds run at 60 seconds to honor Hermes' anti-spin
floor. Operators who need a slower idle backoff set
`autonomous.idle_kick_after_s` explicitly. Disabling autonomy stops future
arming on launch; an already-running Hermes session can be changed immediately
with its native `/heartbeat pause` or `/heartbeat clear` control.

The mechanism is inference-engine neutral. No model alias, provider, endpoint,
or response field participates in the decision; only the selected Hermes TUI
harness and the authored autonomous block do.
