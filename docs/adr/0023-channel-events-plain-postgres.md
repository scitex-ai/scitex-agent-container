# ADR-0023 — `channel_events` moves to PLAIN PostgreSQL, not `scitex_dev.store`

* **Status**: Accepted
* **Date**: 2026-08-28
* **Supersedes nothing. Constrained by**: ADR-0022 (state → PostgreSQL,
  configuration → files under Git).
* **Operator ruling**: 「スクライトなんて全部絶滅させてください」/
  「スクライト1個でも使ったら負け」/ 「止まらずに 0 表になるまで」 — no
  SQLite anywhere, keep going until the table count is zero.

---

## 1. The decision

`channel_events` — the SSE delivery table, and the LAST SQLite table sac
owned (7,968 rows on the host at the time of the move) — becomes two
**plain, sac-owned PostgreSQL tables** in the SAME database
`scitex_dev.store.host_store()` resolves to:

```sql
CREATE TABLE IF NOT EXISTS sac_channel_events (
    target TEXT NOT NULL, id BIGINT NOT NULL, source TEXT,
    kind TEXT NOT NULL DEFAULT 'message', content TEXT,
    meta_json TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL,
    delivered_at DOUBLE PRECISION, PRIMARY KEY (target, id));
CREATE INDEX IF NOT EXISTS sac_channel_events_undelivered_idx
    ON sac_channel_events (target, id) WHERE delivered_at IS NULL;
CREATE TABLE IF NOT EXISTS sac_channel_cursor (
    target TEXT PRIMARY KEY, next_id BIGINT NOT NULL);
```

It does **not** adopt the `scitex_dev.store` record model, which every other
table sac moved in August 2026 did adopt. Target RESOLUTION is still
`host_store` — same database, same `SCITEX_STORE_DSN` override, same
`pg_schema` test isolation, no SQLite fallback — so the operator's rule is
satisfied in full. What is declined is the record/oplog LAYER on top.

Implementation: `_state/state_db_channel.py` (the primitives) and
`_state/state_db_channel_store.py` (DSN resolution, DDL, cached handle).

## 2. Why not `scitex_dev.store` — three disqualifiers, each sufficient

Measured on the live primary during a read-only design pass, before any code
was written.

1. **`PeerState.next_seq()` is O(oplog) per write.** A measured `EXPLAIN`
   shows `Seq Scan` → `GroupAggregate` over the whole oplog, and the store
   NEVER deletes. Minting the SSE cursor through a counter RECORD would pay
   that scan **twice per event** (read-modify-write). The cost of sending a
   message would therefore grow with the number of messages ever sent — on
   every host, forever. This table's whole job is to be written once per
   message.

2. **The oplog sequence is per-ORIGIN, not per-target.** Measured: three
   counters in one store. The SSE `id:` line is a **per-target** cursor, so
   an origin-scoped sequence interleaves two agents' numbering. That is
   exactly the failure `_store_plugin.NEVER_SYNCED` has refused this table's
   replication for since it was written: interleaved numbering silently
   changes what "resume from N" means, and a reconnecting client skips or
   replays frames **with no error anywhere**.

3. **`store.rows()` is the only read primitive** — full decode of every
   record plus a Python-side filter. Measured **16.3 ms at 766 rows** and
   **~190 ms at 9k**. That is paid on every SSE connect, on the event loop,
   for a query the partial index above answers in microseconds.

Any one of these is disqualifying. Together they describe a table whose
access pattern (append-heavy, per-key monotonic cursor, filtered range read)
is the one the record store is not built for.

## 3. What is given up, stated plainly

The store's replication/anti-entropy machinery, `_origin` provenance, HLC
timestamps, and `MergeConflict` reporting. This table refuses replication
anyway (see §2.2), and with ONE shared database there is no per-host copy
for an anti-entropy layer to converge — which is why the `NEVER_SYNCED`
entry is KEPT but its reason is rewritten to *"not a Store schema; one
shared database, nothing to converge"*. The refusal itself is still a design
decision and must not read as withdrawn just because the mechanism changed.

## 4. Exit criterion — what would reverse this

This is a decision, not a fork. `channel_events` comes back INSIDE
`scitex_dev.store` when **both** of the following exist:

1. a **filtered read** that does not decode every record — i.e. a predicate
   pushed into SQL, so `list_undelivered` is an index scan rather than
   `rows()` plus a Python filter; and
2. a **retention verb** that can actually delete — the store's append-only
   oplog is what makes §2.1 quadratic and what makes the retention work in
   §6 impossible inside it.

Either alone is not enough: (1) without (2) leaves the write path quadratic,
and (2) without (1) leaves ~190 ms on the event loop per connect.

## 5. A counter ROW, not `BIGSERIAL` — load-bearing

A PostgreSQL sequence is **non-transactional by design**: `nextval` does not
participate in the surrounding transaction. With two concurrent writers on
one target, id `N+1` can commit and become visible before `N` does. A reader
doing `WHERE id > cursor ORDER BY id` then ships `N+1`, advances its cursor
past it, and **never returns `N`** — a silent drop, no error anywhere, that
SQLite's single serialised writer could not produce.

The counter row makes commit order and id order the same thing. Allocation
and insert share one transaction:

```sql
INSERT INTO sac_channel_cursor (target, next_id) VALUES (%s, 1)
ON CONFLICT (target) DO UPDATE SET next_id = sac_channel_cursor.next_id + 1
RETURNING next_id;
```

The `DO UPDATE` takes a row lock on `(target)` held until commit, so a
second writer for the SAME target blocks until the first has inserted its
event. Writers for DIFFERENT targets touch different rows and never contend
— the serialisation is exactly as narrow as the invariant requires.

Gaps are fine and always were: every reader uses `id > cursor`, never
`id = cursor + 1`. A skipped number costs nothing; a REORDERED one costs a
dropped frame.

### 5.1 `meta_json` is `TEXT` and never routes through a JSON codec

The value stored is the exact `json.dumps(event, ensure_ascii=False)` string
the caller minted. A codec would break byte-identity between the replayed
frame and the live one in three independent ways: `sort_keys=True` reorders
keys, `ensure_ascii=True` mangles Japanese content into escapes, and
`default=str` silently stringifies the values `persist_event` currently
raises on. `jsonb` would additionally normalise whitespace, drop duplicate
keys, and re-order the object. A frame that is *nearly* identical is the
worst available outcome: it looks delivered.

### 5.2 `mark_delivered` requires `target`

Ids were globally unique under SQLite (`INTEGER PRIMARY KEY AUTOINCREMENT`),
so `WHERE id IN (...)` named exactly the rows a stream had just shipped.
They are per-target now, so agent `A` and agent `B` both have an event `1`.
The old predicate would mark `B`'s event delivered while `B` was
disconnected — deleting it from `B`'s fresh-subscriber replay, silently.
Every one of the four call sites has the target name in scope, so requiring
it makes the mistake unrepresentable rather than merely discouraged.

### 5.3 Every DB call inside an async SSE generator is `asyncio.to_thread`

These calls were safe as sync calls while the table was a local SQLite file.
Each is now a network round trip inside `sac listen`, so a blackholed
primary would stall the WHOLE daemon — every request it is serving, not just
one stream. `_listen/_node_channel.py` already fixed this exact hazard once,
for `is_local_node`, when `comms_nodes` moved; this PR copies that pattern
to the channel calls. The DSN also carries an explicit `connect_timeout`
(default 5s), so the thread hop bounds a hang rather than merely relocating
it.

## 6. Retention is needed and is NOT in this ADR's change

7,968 rows, ~413/day/host, and nothing has ever deleted one. Retention is a
BEHAVIOUR change and must land separately, `--dry-run` first.

**Critical constraint, discovered by measurement:** 677 of the 784
undelivered rows belong to target `ci`, which has **never connected**. An
age-based sweep must therefore never touch `delivered_at IS NULL` — doing so
would silently destroy the one thing this table exists to guarantee.

## 7. Data migration preserves ids

`scripts/migrate_channel_events_to_postgres.py`, dry-run by default. All
rows carry over, delivered and undelivered alike, because `list_since_id`
reads regardless of `delivered_at`. Identity is `(target, id)`.

Per-target ids **do not collide across hosts**: a cross-host send is
forwarded to the destination host BEFORE it is persisted, so a given target's
rows only ever exist on one host. `new_id == old_id` for every
non-relocated target. Import is `ON CONFLICT (target, id) DO NOTHING` so a
re-run is idempotent; for any target present on more than one host, the
oldest residency imports first and the later host's ids are offset above the
earlier maximum. `sac_channel_cursor.next_id` is seeded to `MAX(id)` per
target after import. Verification compares count, `min(id)`, `max(id)` and
undelivered count per target against the SQLite source.

### 7.1 The cursor promise holds only if the migration runs FIRST

"A live consumer holding a `Last-Event-ID` resumes exactly" is **conditional,
and the condition is an ordering**: the one-shot must run before the new code
serves that target.

`init_channel_schema` creates the tables lazily on first connect, so the
obvious deploy sequence — restart `sac listen` on the new code, then run the
migration — lets the daemon mint ids from 1 for any target that receives a
message in the gap. Those rows are genuinely new, but an id-shifting importer
cannot tell them from an earlier host's residency, and shifting the migrated
history above them strands both halves: every SQLite-era `Last-Event-ID`
resolves to a *different* event, and the post-cutover rows sit *below* every
live cursor, unreachable through `id > cursor` forever.

**The order is: stop `sac listen` → run the migration → start `sac listen`.**

The script does not rely on anyone remembering. It **refuses**, per target,
when the store already holds a row whose `ts` postdates everything in the
import — a row that cannot belong to an older residency and is therefore the
daemon having moved on. The message names the remedy. The dry run performs
the same check, so the refusal surfaces before the cutover window rather than
inside it. A genuine oldest-residency-first relocation never trips it, and a
negative control pins that.

### 7.2 Re-run idempotence is by CONTENT, not by position

The first version of the re-run probe asked "is the row at the source's own
top id mine?". That is only a valid question when the previous run applied
offset 0. For a **relocated** target the previous run shifted this host's rows
above the earlier residency, so the probe landed on the earlier host's row,
concluded "not mine", and re-imported the entire history at fresh ids — which
`ON CONFLICT (target, id)` cannot stop, because the ids are new. One extra
copy per invocation, while the verification printed `MATCHES SQLite` because
it windows on the shifted range it had just written.

The probe now asks **where this envelope already sits**, confirmed against the
source's first row as well as its last (`meta_json` is not unique — the
at-least-once retry path in §5.3 can duplicate an envelope). Two tests pin it,
including a third invocation, because a bug that adds one copy per run is
invisible to a test that only runs twice.
