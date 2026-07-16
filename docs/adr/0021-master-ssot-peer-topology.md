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

4. **Scope deferred deliberately:** the scheduled `--check` drift alarm
   (the `sac host sync --check --alarm` pattern) lands in PR-C. Peer
   bearer tokens ride this same guarded channel — see §Tokens (PR-B).

## Tokens (PR-B)

Bearer tokens ride the config channel, under the same three-state
honesty. Cross-host a2a needs a bearer in **both directions**, and they
are different secrets (ADR-0020 §5, the per-host blast radius of
`peer_tokens.py`):

- **outbound** — the peer's `peer-tokens/<master>.token` must equal the
  MASTER's listen bearer. What spartan-dev presents when it calls home.
- **inbound** — the master's `peer-tokens/<peer>.token` must equal the
  PEER's listen bearer. What the master presents when it calls the peer.

Either leg can rot alone, and until now both rotted **silently**.
`sac host push-config --check` now reports both (`--no-tokens` opts out)
and folds them into its exit code: a bearer desync is exactly the class
of failure a cron alarm exists for.

### Only digests, ever

Output and JSON carry 12-char sha256 prefixes — never a token value.
This is structural, not a discipline: the read path computes the digest
**on the peer**, so a peer's token value never enters the master's
process at all. Writes carry a value on **stdin** (never the argv, which
the peer's process table exposes to every user on the host) and no
result field holds one. This mirrors `list_peer_hosts`, which has never
returned a value either.

### The rotation contract

`--rotate-tokens <peer>` is single-peer (never `--all`: it restarts the
named peer's listen, and a fleet-wide rotation would drop every peer's
control plane at once) and refuses on any UNDETERMINED state — a peer we
cannot read is a peer we do not rotate. In order:

1. **mint** a fresh bearer (`secrets.token_urlsafe(32)` — the same
   primitive `ensure_token` uses, so a rotated token is indistinguishable
   from a self-minted one);
2. **write both sides** — every candidate listen path on the peer, then
   the master's `peer-tokens/<peer>.token`, retaining the old copy as
   `<file>.pre-rotate-<UTC>`;
3. **restart the peer's listen** — a listen reads its token file **once,
   at boot**, so until the process restarts the write is inert and the
   master would hold a bearer the peer does not honour;
4. **verify** with a falsifiable authenticated probe;
5. **only then discard** the pre-rotate copy.

Every failure leg names **which side now holds what** and keeps the
backup. The two sides are never left silently split.

### Why the verification probe is two requests

`/v1/health` is in `BearerAuthMiddleware.PUBLIC_PATHS` — it answers 200
to *any* bearer, including a stale one. Verifying a rotation against it
would be a check that passes whether or not the rotation worked. The
probe therefore hits the authenticated `GET /agents`, and it runs
**twice**: the new bearer must be ACCEPTED *and* a freshly minted bogus
one must be REJECTED. A single positive cannot distinguish "the listen
adopted our token" from "this listen admits everything", and this is the
one step where being wrong silently splits the fleet's credentials.

### The FQDN fix, and what is NOT yet wired

`tokens.default_token_path()` keys the file on `socket.gethostname()`.
On a multi-login-node cluster that name is **not stable**: a listen
restarted on spartan-login2 reads a different file than one started on
spartan-login1, mints a fresh bearer when that file is missing, and the
master's copy silently stops matching. So:

- the generated client config carries
  `listen.token_file: ~/.scitex/agent-container/tokens/listen-<peer>.token`
  — keyed on the **canonical name**, the one identity that does not move
  when a login node does;
- a rotation seeds **both** that stable path and the hostname-keyed one
  the listen reads today, with the same value. Whichever file the listen
  picks up, it comes up holding the rotated bearer — the ambiguity is
  collapsed by construction rather than by hoping;
- `--check` reports every `listen-*.token` it finds with its digest. If
  they **disagree**, the verdict is UNDETERMINED, not a guess: the listen
  may have booted on a node this ssh did not reach, and picking the file
  that happens to match the master's copy would be an answer chosen to
  agree with us.

**`sac listen` does NOT read `listen.token_file` yet** — it resolves its
token from `--token-file`, else the hostname-keyed default. Wiring the
boot path to read this key is deliberately NOT in PR-B (see
Consequences). Until a peer's launcher passes `--token-file`, the key
records **where the rotated bearer is**, not what the listen reads, and
the generated config says so in a comment beside it.

## Consequences

- ADR-0020's manual placement steps 5–6 collapse into commands run from
  the master: `sac host push-config <peer>` replaces the hand-typed
  peers block (step 6), and `sac host push-config <peer> --with-tokens`
  replaces the hand cross-registration of the master's bearer (step 5's
  `ssh spartan cat <token> > …` dance). The peer's OWN bearer is minted
  and distributed by `--rotate-tokens <peer>`, which additionally does
  what the manual procedure never did: restart the peer's listen and
  PROVE it adopted the token.
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
- **`--check` now exits non-zero on token drift too** (PR-B). This is a
  deliberate widening of PR-A's exit contract: a bearer desync is
  silent, breaks a2a, and is precisely what the PR-C timer will alarm
  on. `--no-tokens` restores the config-only behaviour.
- **Wiring `sac listen` to READ `listen.token_file` is deferred**, and
  not for tidiness. `listen_cmds._do_start_listen` resolves
  `token_file or default_token_path()` and calls `ensure_token`, which
  **mints on missing**. So a config carrying the stable path, landing on
  a peer whose stable path is not yet seeded, would make the next listen
  restart mint a fresh bearer and desync the master — the very failure
  this section exists to remove, re-introduced through the fix. Three
  further facts make it a design decision rather than a small edit:
  `Config` parses no `listen:` block at all (`host` / `peers` / `lead`
  only); `_restart.restart_listen` relaunches with a bare
  `[sac_binary(), "listen"]` argv, and the systemd path re-executes the
  unit's own `ExecStart`, so what a restarted listen reads is decided by
  the LAUNCHER, not by this key; and the boot path is one the codebase
  guards hard ("the bind must be impossible to block"), where a new
  config read is a new way for the fleet's control plane to fail to
  start. Until that is designed, the rotation's write-both-paths rule
  makes the ambiguity harmless anyway, which is why PR-B ships without
  it.

## References

- ADR-0013 — central propagating fleet registry (master-authoritative
  instances story).
- ADR-0014 — symmetric federated comms graph (superseded here on the
  topology-config point only).
- ADR-0015 — cross-host push ssh transport.
- ADR-0020 — cross-host Spartan agent placement (steps 5–6 are the
  manual procedure this ADR mechanises).
