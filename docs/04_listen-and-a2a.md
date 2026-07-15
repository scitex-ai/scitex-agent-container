# 04 — Listen & A2A

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> Consolidates [`talking-to-agents.md`](talking-to-agents.md) and the
> control-plane half of [`how-sac-works.md`](how-sac-works.md).

## Scope

The per-host `sac listen` control plane and the agent-to-agent (A2A) mesh built
on top of it. `sac listen` is the plane every agent reaches its host through and
the broker for spawns and pushes; A2A (`POST /v1/turn`) is how one agent reaches
another — locally or, via peered listens, across hosts by name.

## TODO — this page will contain

- [ ] `sac listen` lifecycle: `start` / `status` / `stop` / `restart`, the loopback-only default bind (`127.0.0.1:7878`), the bearer token file, and `--allow-non-loopback`.
- [ ] Why bare `sac listen` is deprecated (removed in v0.23.0) in favour of `sac listen start`; the single-instance flock guard.
- [ ] The control-plane routes (`/v1/health`, `/v1/sac/agents`, per-agent `status`/`send`/`card`) and how the live-runner route vs the `claude --resume` re-launch fallback are chosen.
- [ ] The three transports for reaching a running agent, and when to use each: A2A `POST /v1/turn`, `sac agents send`, and the host-level `sac listen` — with copy-pasteable `curl`.
- [ ] `sac peer post-turn <agent> "<msg>"` and `sac peer resolve-url`; the generic `sac a2a` surface (`serve`, `doctor`).
- [ ] Name resolution through the federated `comms_nodes` registry (agent → `{host, a2a_port}`); `sac registry` / `sac db` reconciliation.
- [ ] The A2A ACL model: `sac a2a grant` / `revoke` / `block` / `unblock` / `grants`.
- [ ] Cross-host forwarding: each host's listen peers to the others over SSH-`curl` (→ [03](03_cross-host-control-plane.md)); running `sac listen` as a systemd-user service.
- [ ] The one-way `sac` → `scitex-orochi` relationship (→ [sac-and-orochi.md](sac-and-orochi.md)).

<!-- EOF -->
