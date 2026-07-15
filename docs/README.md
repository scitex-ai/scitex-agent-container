# scitex-agent-container — Documentation

Product documentation for `sac`. Start here, then follow the numbered guides
in order or jump to the deep-dive you need.

> **Status (2026-07-16):** the numbered `NN_*.md` pages below are the planned
> product-doc skeleton. Several are **stubs** (scope + TODO) that consolidate
> existing deep-dive docs already in this directory — each stub names the
> doc(s) it will fold in. The deep-dive docs remain authoritative until a stub
> supersedes them.

## Guides (read in order)

| # | Page | Scope |
|---|------|-------|
| 01 | [Architecture](01_architecture.md) | The big picture: `spec.yaml` → runtime → host control plane. |
| 02 | [Agent spec](02_agent-spec.md) | The `spec.yaml` schema (v3), field by field. |
| 03 | [Cross-host control plane](03_cross-host-control-plane.md) | Master-authoritative placement, `host:`-field routing, multi-hop SSH, cross-host list/attach. |
| 04 | [Listen & A2A](04_listen-and-a2a.md) | The per-host `sac listen` control plane and the agent-to-agent mesh. |
| 05 | [Credentials & accounts](05_credentials-and-accounts.md) | The credential/account pool, quota, and rotation. |
| 06 | [CLI reference](06_cli-reference.md) | Every command group and flag. |

## Deep-dives (authoritative reference)

- [how-sac-works.md](how-sac-works.md) — single-agent launch flow, `to_home/` merge rules, A2A inbound.
- [spec-reference.md](spec-reference.md) — annotated full `spec.yaml` + field table.
- [talking-to-agents.md](talking-to-agents.md) — the three transports for reaching a running agent.
- [isolation.md](isolation.md) — Apptainer leak paths + sac's hardened-by-default countermeasures.
- [images.md](images.md) — `base` vs `scitex` image layers; sandbox / freeze / version pinning.
- [directories.md](directories.md) — the runtime/config directory tree and the config cascade.
- [credentials.md](credentials.md) — credential storage and refresh.
- [deploy-runbook.md](deploy-runbook.md) — host deploy / upgrade runbook.
- [sac-and-orochi.md](sac-and-orochi.md) — the sac ↔ scitex-orochi responsibility split.
- [adr/](adr/) — Architecture Decision Records (0001–0020). ADR-0020 is the cross-host placement runbook.

## Planned

- **`examples/`** — a curated set of runnable example specs and a guided tutorial series (see the repo's [`examples/`](../examples/)). A docs landing page for it is planned.

<!-- EOF -->
