# ADR-0021 — Master-SSOT peer topology: generated client configs, pushed one-way

## Status

Accepted (2026-07-16, operator-approved). PR-A implements the config
channel; bearer-token distribution (PR-B) and the scheduled drift timer
(PR-C) follow on the same channel.

## Context

The fleet's cross-host control plane is **master-authoritative**: the
master (ywata-note-win) holds the sole registry, peers are pure clients,
and definitions live only on the master (ADR-0020's standing
constraint; operator ruling 2026-07-16). But peer-side
`~/.scitex/agent-container/config.yaml` files were still **hand-written
per host**, and each one has already drifted its own way:

- Spartan's `comms_nodes` / `peers` block was typed by hand during the
  spartan-dev placement (ADR-0020 step 6) and nothing keeps it aligned
  with the master since.
- mba's config is an orochi-era relic (a symlink into the retired
  orochi-shared layout carrying stale topology).
- Nothing DETECTS any of this. A wrong `host.canonical` silently
  mis-scopes every state.db write on that host; a wrong reverse route
  silently breaks peer→master a2a.

ADR-0014 framed the comms graph as **symmetric** — every host holds and
writes its own registry state. That framing is superseded *on the
topology-config point*: hosts still each run a listen and forward as
peers, but the *configuration describing the topology* now has exactly
one author, the master.

## Decision

1. **The master's config.yaml is the fleet's only hand-edited topology
   file.** Peers get a GENERATED minimal client config: their canonical
   name (pins state.db identity), `comms_nodes.sync_on_start: false`
   (ADR-0020's startup-hang guard), and one `peers:` entry — the route
   BACK to the master (`PeerSpec.reverse_ssh` in the master's config,
   defaulting to the master's name). Deliberately NOT emitted: `via:`
   chains, `env_preamble`, glob templates, other peers — peer-side
   readers do not consume them, and emitting them would re-create
   distributed topology.

2. **`sac host push-config <peer> | --all`** renders and reconciles,
   one-way master → peer, as the sibling of `sac host sync` (same
   registration, same `--check`/`--json` shape, same three-state
   honesty): CURRENT / STALE_GENERATED / ABSENT are reconciled or
   reported; UNDETERMINED never mutates and never reads as clean; every
   push is verified by reading the peer back byte-for-byte.

3. **Refuse-on-hand-edit.** A peer file WITHOUT the generated header is
   never overwritten: the verb prints the unified diff and stops;
   `--adopt` (single peer only) replaces it after backing it up on the
   peer (`config.yaml.pre-adopt-<UTC>`). Symmetrically, `sac host
   add/set/remove` REFUSE to CRUD-edit a config that carries the
   generated header — on a client host the fix belongs on the master.

4. **Scope deferred deliberately:** peer bearer tokens (ADR-0020 step
   5) ride this same guarded channel in PR-B; the scheduled `--check`
   drift alarm (the `sac host sync --check --alarm` pattern) lands in
   PR-C. This ADR ships the config channel only.

## Consequences

- ADR-0020's manual placement steps 5–6 collapse into two commands run
  from the master: `sac host push-config <peer>` (this ADR) and the
  PR-B token-push verb.
- ADR-0014's symmetric framing is superseded on this point: topology
  configuration is authored once, on the master. The runtime comms
  graph (every host runs a listen; forwards are peer-to-peer) is
  unchanged.
- A generated config is byte-comparable, so drift detection is exact
  (`--check` exits non-zero; the volatile push-timestamp line is the
  one ignored difference). Hand edits on peers stop being silent: the
  checker shouts and the next push refuses until `--adopt`.
- The mba orochi-relic symlink is retired by adoption: `--adopt` backs
  up the linked content on the peer, and the atomic `mv` lands a
  regular generated file in its place.
- The master itself is protected organically: its own config carries no
  generated header, so a misdirected push against it lands in the
  HAND_EDITED refusal, and the CLI additionally refuses a peer name
  equal to the local canonical host.

## References

- ADR-0013 — central propagating fleet registry (master-authoritative
  instances story).
- ADR-0014 — symmetric federated comms graph (superseded here on the
  topology-config point only).
- ADR-0015 — cross-host push ssh transport.
- ADR-0020 — cross-host Spartan agent placement (steps 5–6 are the
  manual procedure this ADR mechanises).
