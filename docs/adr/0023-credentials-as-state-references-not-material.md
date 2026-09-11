# ADR-0023 — Credentials are STATE: the database records references, never material

* **Status**: Accepted (first slice implemented)
* **Date**: 2026-08-12
* **Builds on**: ADR-0017 (one account, one refresher), ADR-0022 (state →
  PostgreSQL, configuration → git)
* **Operator framing**: credentials are currently *facts about a
  filesystem* — discoverable only by looking, unrecorded, and lost or
  duplicated by any host change.

---

## 1. The failure this must make impossible

Three incidents, one root cause.

1. **Eight subagents died simultaneously on expired credentials.** At
   that moment `sac.accounts-refresh.timer` had fired 23 minutes earlier
   and was scheduled normally, and `account_show` reported the account
   healthy at 5h 32% / 7d 44%. **A timer running is not evidence that a
   token is usable**, and nothing in the system held the actual fact.
2. **The operator's Telegram channel vanished during a relocation.** The
   only working credential path on the old host was a token folded into
   `$HOME/.env` by an `.envrc` cascade. Relocation carries neither `.env`
   nor credential material — correctly — so the one path that actually
   worked disappeared at the moment of the move, and nothing anywhere
   recorded that it had been the working path.
3. **A forge token sits in plaintext in a `~/.bashrc`**, found only
   because somebody was looking at that file for another reason.

None of these is a *distribution* failure. All three are *knowledge*
failures.

## 2. Decision

**The database records credential FACTS. It never records credential
MATERIAL — not the secret, and not a digest of it.**

A row is a **credential descriptor**: which account, which node is
primary, which tier, when minted, when it expires, and a *locator* — a
scheme-prefixed reference (`file:<abs path>`, `env:<VARNAME>`) that makes
the material findable, refreshable and auditable without being present.

## 3. Why not store the material — argued, not inherited

**3.1 It does not address any of the three incidents.** Decisive on its
own. In (1) the token existed and was reachable; what was missing was the
fact that it was *unusable*. In (2) the material was intact on the old
host; what was lost was the *knowledge that this was the working path*.
In (3) the material is present and working; the defect is that nobody
*declared* it. Storing bytes fixes none of the three. Storing facts fixes
all three. A design that adds exposure while addressing none of the
motivating failures is a strictly worse trade whatever else it offers.

**3.2 Blast radius follows replication, and these rows are built to
replicate.** ADR-0022 §5 exists precisely to make them travel. Four
clones of the cards store exist right now. A secret in a synced table is
a secret in every store the row reaches, in each of their WALs — postgres
never overwrites in place, so a rotated secret persists in the heap until
`VACUUM` — and in every base backup taken meanwhile, permanently.
Rotation cannot retract it. File exposure is bounded and auditable with
`stat`; this is neither.

**3.3 It would silently repeal the two-tier invariant rather than
preserve it.** Today's guarantee is structural: `_account.mint_token`
strips `refreshToken` by construction, and
`_keepalive_guards.assert_access_only` re-scans the payload at every
depth before it leaves the host — on the stated grounds that *"a guard
that only runs when the stripper is correct guards nothing"*. That guard
sits on ONE rail: the ssh keepalive push. A table holding material opens
a SECOND rail that the guard does not sit on, and it is the quieter one:
`assert_access_only` raises; `INSERT` does not. The invariant would stay
true of the rail nobody leaks through and become false of the new one.

**3.4 What the reference model costs is recoverable; what a leak costs is
not.** The only thing material-in-database buys is a replica serving
itself without contacting its primary. A purpose-built rail for that
already exists — `keepalive_push`, which verifies HTTP 200, publishes at
mode 0600, and refuses to downgrade a working remote credential.
Replacing a verified push with an unverified pull is a downgrade sold as
a feature.

### 3.5 Not even a fingerprint

A digest in a replicating table is a verifier that travels to every host
receiving the row, and it buys only *local* drift detection ("did
something replace this file behind my back"). Cross-host staleness is
answered by `generation` instead, which leaks nothing. If drift detection
later needs more, the right shape is a node-local column excluded from
the sync projection — not a digest that replicates.

### 3.6 Enforced, not conventional

`_credstate/_material.py` scans every row on the only path into the store
— by field name *and* by value shape (provider prefixes, JWT, PEM,
Telegram, and a conservative high-entropy rule) — and REFUSES the write
if anything is secret-shaped. It is the same guard as
`assert_access_only`, re-erected on the new rail, for the same reason.
The refusal message never quotes the offending value, because that
message reaches transcripts.

## 4. Schema

Three tables, all carrying the five ADR-0022 §5.1 sync columns
(`row_uuid`, `origin_node`, `revision`, `updated_at`, `deleted_at`) from
creation.

| Table | Holds | Conflict class (§5.2) |
|---|---|---|
| `credential_descriptor` | what exists and who owns it | `shared_mutable` |
| `credential_placement` | where it is supposed to be | `shared_mutable` |
| `credential_observation` | what was actually seen, and when | `log` |

**Why descriptor/placement are `shared_mutable`, not `state`.** Any host
may edit them, and primacy *handover* is exactly the edit whose conflicts
must never be auto-merged: a blind last-write-wins would let a
clock-skewed replica seize primacy and produce two refreshers — the
precise mutual-invalidation failure the two-tier model exists to prevent.
Rule 4 applies: accept a remote row only when
`remote.origin_node == local.origin_node AND remote.revision >
local.revision`; otherwise DO NOTHING and write a divergence record.

**Why observation is `log`.** It is append-only and `origin_node` is in
the PRIMARY KEY, so two nodes can never author the same row and a
collision provably means the identical row arrived twice.

**`generation` is not `revision`.** `revision` is the ROW's version,
bumped by any metadata edit. `generation` is the MATERIAL's version,
bumped only on mint or rotation. Conflating them would make "somebody
fixed a typo" indistinguishable from "the token was rotated", and only
the second invalidates every replica.

`_credstate/_contract.py` is the base-table helper ADR-0022 §7.3 records
as missing: it emits the five columns and REFUSES a `CREATE TABLE` that
lacks them, is wrongly typed, makes the tombstone non-nullable, or (for
`log`) omits `origin_node` from the primary key. It fails closed — a DDL
it cannot parse is rejected, because a table that slips through can never
be synchronised without a rewrite. It is generic and knows nothing about
credentials, so any consumer stream can use it.

## 5. CR-001, made checkable

**CR-001 — for any account, exactly one node holds refresh material.**
More than one is mutual invalidation: an OAuth refresh rotates the
refresh token, so whichever host refreshes first silently revokes the
other.

Today this invariant is conventional *and invisible*.
`_keepalive_guards.holds_refresh_material` infers the holder from whether
a file happens to contain a field, so a second holder appearing looks
exactly like the first one. An inference cannot contradict itself.

Declaring `primary_node` makes it a fact that can be WRONG, and both
halves then become mechanical:

* **the fleet half** — a SQL query (`CR001_MULTIPLE_PRIMARIES_SQL`):
  more than one declared primary per credential;
* **the disk half** — declared role versus observed refresh material
  (`_verdict.check_single_refresher`), which names `EXTRA_REFRESHER`
  out loud, and `NO_REFRESHER` when nothing in the fleet can renew.

## 6. What "materialize" means

`sac creds status` answers, per credential, per node: `OK` / `ABSENT` /
`EXPIRED` / `EXPIRING` / `UNRESOLVABLE` / `WORLD_READABLE` /
`EXTRA_REFRESHER` / `NO_REFRESHER` / `UNDECLARED` — with a remedy
attached to every fault, and a non-zero exit.

For a `distributable` credential the remedy names the command that
materializes it from its primary. **For a `primary_secret` on a
non-primary node the answer is that it must NOT be materialized here** —
the remedy says so and names what to mint instead. That refusal is the
two-tier model holding, not a gap: a `materialize` verb that offered to
copy refresh material would quietly repeal the invariant this design is
required to preserve.

Two verdicts are new, and both are shapes that were previously silent:

* `EXTRA_REFRESHER` — a node holds refresh material the fleet never
  declared it to hold (the CR-001 violation);
* `UNDECLARED` — credential material exists that no row describes. This
  is the shape of the `~/.bashrc` token and of the relocated `$HOME/.env`
  path: working, load-bearing, and recorded nowhere.

Nothing goes green because a schedule ran. Every verdict is a comparison
against a measurement of the artifact itself, and every measurement is
presence-only — never the value, never its validity, because probing
whether a refresh token still works is a WRITE (a rejected refresh
CLEARS the field).

## 7. Deferred, and honestly named

* **Sync is not implemented.** The columns and the per-class rules exist
  so that it *can* be; the primitive itself lives in `scitex_dev.state`,
  which does not exist yet. Cross-host isolation today is expected.
* **`_credstate/_store.py` is a temporary home.** ADR-0022 §4 places the
  postgres API in `scitex_dev.state` (`dsn_for`, `write_transaction`,
  `store_identity`). This module is written to that shape — refuses 5432,
  refuses to guess, keeps the password out of the DSN, asserts schema
  once per store — so it lifts out unchanged when the real one lands.
* **No production database is created by this change.** The tables are
  asserted on first `open_store`; the DSN must be configured
  (`SCITEX_AGENT_CONTAINER_STATE_DSN`) and is never defaulted.
* **Discovery of undeclared material is a function, not a scanner.**
  `undeclared_findings` takes an observed set; nothing yet walks a host
  hunting for credential-shaped files. That scanner is the natural next
  slice and is where incident (3) actually gets closed.
* **Handover of primacy is not yet a verb.** It is representable
  (`primary_node` + a divergence report) but the deliberate two-step
  hand-off is unimplemented.
