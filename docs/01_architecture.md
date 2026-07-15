# 01 — Architecture

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> Consolidates and extends [`how-sac-works.md`](how-sac-works.md).

## Scope

The big picture of `sac`: how a single `spec.yaml` (plus an optional `to_home/`
overlay) is materialized into a long-lived, externally addressable Claude agent,
and how the pieces — spec loader, runtime adapter, state DB, and the per-host
`sac listen` control plane — fit together. This page is the map; the numbered
guides that follow (02–06) drill into each region.

## TODO — this page will contain

- [ ] A layered component diagram: spec loader → config (`AgentConfig`) → runtime adapter (apptainer / tui / docker / podman) → running Claude session.
- [ ] The launch pipeline end to end (`sac agents start`): resolve spec → preflight (`check`/`explain`) → `to_home/` materialization → runtime dispatch → registry row.
- [ ] The four on-disk surfaces and who owns each: `agents/<name>/` (spec SSoT), `runtime/<name>/` (live state: pid, heartbeat, `session.jsonl`), `containers/` (SIFs), and `state.db` (registry).
- [ ] Where the control plane sits: `sac listen` per host, A2A `POST /v1/turn` per agent, and the boundary to the cross-host plane (→ [03](03_cross-host-control-plane.md)).
- [ ] The dir-as-SSoT principle (no hidden state) and how it makes agents reproducible and diff-reviewable.
- [ ] Pointers into the module layout under `src/scitex_agent_container/` (`config/`, `_lifecycle/`, `runtimes/`, `_state/`, `cli_pkg/`).

<!-- EOF -->
