# ADR-0020 — Cross-host (Spartan) containerized-agent placement + bidirectional a2a

## Status

Accepted (2026-07-16). Implemented while placing `spartan-dev`, the first
containerized agent on Spartan HPC. Builds on ADR-0014 (federated
`comms_nodes`) and ADR-0015 (cross-host ssh-curl push transport).

## Context

The fleet needed its first **containerized agent on a remote host** (the
Spartan HPC login node): `spartan-dev`, whose job is to keep Spartan's
software current. This exercises every seam of remote placement at once,
under the operator's standing constraints:

- **Definitions live only on the master.** Agent specs live in
  `~/.dotfiles/src/.scitex/agent-container/agents/<name>/spec.yaml` on
  ywata-note-win (the master). Nothing definitional is written to Spartan
  (operator doctrine, 2026-07-12). Spartan is a **pure client**.
- **No writable scitex-todo board on Spartan.** Spartan's
  `~/.scitex/todo/tasks.yaml` is a *different* board; a naive write there
  cannot merge with the master's fleet board (no shared FS, flock does not
  span GPFS). A Spartan agent must not carry `server:scitex-todo` unless it
  is pointed at a shared store.
- **Per-host `sac listen`.** sac's cross-host a2a is *not* "point an agent
  at a remote listen" — it is **each host runs its own listen, and listens
  forward to each other as peers** over ssh-curl (ADR-0015), keyed on
  `host_config.peers` + a per-host peer-token registry (`peer_tokens.py`).

The naive `sac agents start spartan-dev` failed with a chain of blockers,
each surfaced empirically (never assumed):

1. Stale/absent Spartan SIF → built fresh on the compute node.
2. No fleet creds on Spartan → the agent could not authenticate.
3. No `sac listen` on Spartan → `server:sac` had no bus token → sac
   *correctly* refused to launch an agent whose adapter can never subscribe.

## Decision

Place a remote containerized agent via the pipeline below — all driven from
the master; Spartan stays a pure client holding **no definitions** (only a
runtime SIF binary, host-local comms config, secrets, and the running
process).

1. **host-field routing.** The spec declares `host: spartan`;
   `sac agents start` ssh-dispatches to the login node and runs
   `sac agents start <name> --no-redispatch` there. Requires an
   `env_preamble` under `host_config.peers.spartan` (PATH to
   `~/.env-3.11/bin`, lmod init, module loads) — without it the
   non-interactive `ssh spartan -- sac …` cannot find `sac` and dies rc=127.

2. **Build the SIF on the compute node.** `sac image build base && scitex`
   inside the CPU lease via `srun --jobid=<lease> --overlap`, with
   `TMPDIR=/tmp APPTAINER_TMPDIR=/tmp APPTAINER_CACHEDIR=/tmp/apptainer-cache`
   (node-local scratch). The default `TMPDIR` resolves to GPFS at ~95%
   inodes and would exhaust it mid-build. Output lands at
   `~/.scitex/agent-container/containers/sac-scitex.sif` (the spec's
   `apptainer.image` path). **Verify the artefact** (`scitex_todo==0.13.5`,
   `yaml.__with_libyaml__ is True`) by running it — never trust `rc=0`.

3. **Distribute creds master→Spartan.** `rsync` the `.credentials.json`
   files from the master's `~/.scitex/agent-container/accounts/*/` to
   Spartan's same path (0600 preserved). Locally, agents read creds via the
   whole-home bind; a remote host has no such bind, so the master must
   **distribute** them (operator: "master から配らないと意味が無い"). Ongoing
   freshness (tokens expire) = the auth-SSOT sync loop, still TODO.

4. **Run `sac listen` on the Spartan login node.**
   `setsid nohup ~/.env-3.11/bin/sac listen …` (prefer `sac listen start`;
   bare `sac listen` is deprecated). This mints
   `tokens/listen-<spartan-fqdn>.token` — exactly the bus token the agent's
   `server:sac` adapter needs — and serves the local bus the agent
   subscribes to. With no `peers:` yet, startup peer-sync has nothing to
   reach, so it cannot hang.

5. **Cross-register peer-tokens** (WI-4 per-host bearer registry, ADR-0015).
   Master gets `peer-tokens/spartan.token` (Spartan's listen bearer);
   Spartan gets `peer-tokens/ywata-note-win.token` (the master's). Use
   `sac host add-peer <host> <token>`, or copy **host-side**
   (`ssh spartan cat <token> > master/peer-tokens/spartan.token`) so the
   secret never transits your own context.

6. **Spartan `config.yaml` peers block** (host-local comms config, NOT a
   definition):
   ```yaml
   comms_nodes:
     sync_on_start: false   # avoid the no-timeout startup-sync HANG
   peers:
     ywata-note-win:
       ssh: ywata-note-win  # must resolve Spartan→master (confirmed via overlay)
   ```
   The forwarder loads this per-request, so no listen restart is needed for
   it to take effect on forwards.

7. **`lead` registry entry on Spartan** (final step for dev→master):
   `resolve_node_host('lead')` returns None until Spartan has a
   `comms_nodes` row for the master node — the Spartan listen warns about
   this at startup ("no `lead:` block"). Add a `lead:` block to Spartan's
   `host_config`, or run `sac registry sync` from the master.

## Consequences

- **spartan-dev runs on Spartan**, authenticated with the distributed creds
  (`claude` process live), a2a server on `127.0.0.1:19002`, tmux session
  `tui-spartan-dev`. Steps 1–6 verified end-to-end 2026-07-16.
- **master→dev a2a works**: the master resolves spartan-dev and forwards
  ssh-curl to `:19002` with the `spartan` peer-token.
- **dev→master a2a**: transport (ssh + peer-token + config) is in place and
  Spartan→master is reachable (`ssh ywata-note-win` succeeds from Spartan);
  the remaining piece is step 7 (the `lead`/registry resolution).
  **Gotcha:** the `sac agents send` CLI resolves the target's *spec*
  locally, so it cannot be driven cross-host from Spartan (specs are
  master-only) — the agent's own `server:sac` channel is the report path,
  not the CLI.
- **Security**: fleet creds now live on a shared HPC (0600). Accepted by the
  operator (his infra/creds). Freshness needs the auth-SSOT loop.
- **Reproducible**: this is a runbook. Placing the next remote agent
  (e.g. neurovista on the GPU lease) reuses steps 1–7; only the SIF's lease,
  the workdir, and the agent name change.
