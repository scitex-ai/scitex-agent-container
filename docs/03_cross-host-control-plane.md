# 03 — Cross-host control plane

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> This is the **newest** subsystem (PRs #705–#711, 2026-07-16). The
> authoritative runbook today is
> [ADR-0020](adr/0020-cross-host-spartan-agent-placement.md); this page will
> lift it into product-doc form.

## Scope

How one **master node** (`ywata-note-win`) owns every agent definition and the
authoritative registry, yet places and drives agents on other hosts — a laptop,
an HPC login node, or (via multi-hop SSH) a compute node. The master is the only
place specs live; peers are pure clients holding only a runtime SIF, host-local
comms config, credentials, and the running process.

## TODO — this page will contain

- [ ] The master-authoritative model: specs live only on the master; peers hold no definitions ("master から配らないと意味が無い").
- [ ] `spec.host:` routing — the three outcomes (`local` / `remote` / `unknown`) and why an unroutable host is a loud error, never a silent local start (`cli_pkg/lifecycle/_dispatch.py::try_dispatch`).
- [ ] The dispatch pipeline for a remote `sac agents start`: drift-check (`rsync --dry-run --itemize-changes`) → rsync spec dir → `sac agents start <name> --no-redispatch --json` over SSH → master-side `state.db` `instances` + `comms_nodes` row.
- [ ] Multi-hop placement: `host: spartan-bmNNN` reaches a compute node via OpenSSH `ProxyJump` (`-J`) from the peer's `via:` chain (`_state/_host_ssh.py::build_ssh_argv`); glob peers inherit the login node's pinned state root.
- [ ] Cross-host liveness: `sac agents list` live-probes each remote agent on its own host; the non-login-shell tmux probe (PR #709) that keeps a healthy session from reading DEAD; honest `UNKNOWN` verdicts (PR #711).
- [ ] Cross-host `attach` (PR #707) and node-aware `restart` over SSH.
- [ ] Peer registration: `sac host add-peer` / `list-peers`, the per-host listen bearer tokens, and cross-registration.
- [ ] The `env_preamble` requirement on HPC peers (Lmod init, PATH to `sac`) and the `bash -c` (not `-lc`) wrapper that avoids compute-node bashrc kills.
- [ ] Worked example: placing `spartan-dev` on the Spartan login node, end to end.
- [ ] Split-brain safety: why a remote agent must not write a non-shared `scitex-todo` board, and reports outbound via a2a instead.

## Related

- [ADR-0020 — cross-host (Spartan) agent placement](adr/0020-cross-host-spartan-agent-placement.md)
- [ADR-0013 — central fleet registry](adr/0013-central-fleet-registry.md)
- [ADR-0014 — symmetric federated comms graph](adr/0014-symmetric-federated-comms-graph.md)
- [ADR-0015 — cross-host push SSH transport](adr/0015-cross-host-push-ssh-transport.md)
- [04 — Listen & A2A](04_listen-and-a2a.md)

<!-- EOF -->
