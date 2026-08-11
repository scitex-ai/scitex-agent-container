# ADR-0022 — States → PostgreSQL, Configuration → files under Git

* **Status**: Proposed (first slice implemented)
* **Date**: 2026-08-12
* **Operator rulings**: 「state.db というものは使ってはいけません」/
  「sqlite 使った瞬間負けだと思った方が良いです」/
  「今 5432 を scitex のために使ってるものはすべて間違い」/
  「今他のホストと連絡が取れてないっていうのはまぁ正常です。正しいです」
* **Consumers**: scitex-agent-container, scitex-cards, scitex-scholar,
  scitex-writer, scitex-plt (figrecipe), scitex-hub, scitex-ui,
  scitex-app —「いろんなパッケージから使う」

---

## 1. The failure this must make impossible

Three incidents, all the same bug wearing different clothes:

1. **`sac agents relocate --to scitex-compute-03` → nine simultaneous
   `403 ACL deny`.** `host_exec` resolves a caller's group from
   `node_comms_policy`, written at `agent_start`. compute-03 had never
   started that caller, so it had no row. *Authorization became a
   function of where an agent had previously happened to run.*
2. **A relocated agent's registry row does not exist on the target**, so
   `sac agents list <name>` fails there while `list --json` includes it.
3. **`sac agents list` returns 111 agents inside a container and 126 on
   the bare host.** On 2026-08-09 three agents concluded the fleet
   registry had been wiped and two escalated it as P1. The host DB was
   healthy.

The common shape is not "SQLite is slow". It is: **a fact was cached
into a store whose identity depends on where the reader is standing.**

---

## 2. The intended topology

Corrected per the operator, 2026-08-12. This ADR previously proposed a
single central primary; that was wrong.

* **Port 5432 is never used for scitex.** Any code, spec, or default
  naming it is a defect. (Purging it from specs is Wave A2's task, not
  this one.)
* **Port 55432 — the containerized PostgreSQL 18 — runs on every
  registered host, deliberately.** Per-host instances are the design,
  not an accident to be collapsed. A node must remain fully functional
  while isolated: writes are local and never block on a peer.
* **The per-host instances are then SYNCHRONIZED.** That synchroniser is
  this primitive. It lives in scitex-dev and is consumed by the leaves.
* **Today's cross-host isolation is therefore EXPECTED, not a bug.** The
  gap is the absence of the sync layer, not the presence of separate
  databases.

### Measured, 2026-08-11 — what isolation currently costs

Four `scitex_cards` stores, all descended from one `pg_restore`, all
still live, none synchronised (0 publications, 0 subscriptions):

| endpoint | `count(*) FROM tasks` | `max(last_activity)` |
|---|---|---|
| ywata-note-win `:55432` | 3843 | 2026-08-11T18:28:28Z |
| scitex-compute-03 `:55432` | 3719 | 2026-08-10T11:25:12Z |
| scitex-compute-04 `:55432` | 3743 | 2026-08-11T18:59:58Z |
| nas via `:5442` | 3425 | 2026-08-11T21:48:15Z |

Three findings that must shape the design:

* **The existing identity check cannot see a fork.** All four carry the
  same `schema_meta.store_uuid` (`1d55dd6e-…`) because they are clones
  of one dump. The PostgreSQL **cluster** identifier does differ —
  `SELECT system_identifier FROM pg_control_system()` gives
  `7671108644284358700` vs `7672112238472680366` where `store_uuid` is
  identical. Identity must be the **pair**.
* **The divergence reaches inside a single agent, and that part is a
  real defect.** In this container `$SCITEX_CARDS_DB` names `:55432`
  while the cards MCP server process resolved `:5442` (the NAS tunnel).
  Verified by writing a card through the MCP tool: it appeared in
  `:5442` (3424 rows) and was absent from `:55432` (3743 rows). One
  agent, two stores, no error. This is not the expected cross-host
  isolation — it is one process disagreeing with its own sibling.
* **Same-host counting, this host**: of 116 `spec.yaml` files, 92 set
  `SCITEX_CARDS_DB` — 90 at `:55432`, 2 at the closed `:5432`.

Also correcting the brief: on **scitex-compute-04** the state DB is
3.97 MB (the 271 MB figure is ywata-note-win's), there are 4012
`state.db` files, and `~/.scitex/agent-container/state.db` does not
exist here. The `resolves to group ''` message was already rewritten on
2026-08-09 to distinguish "no row here" from "registered and ungrouped";
the *diagnostic* improved, the *cause* did not.

---

## 3. The cut: three kinds of fact, three homes

Not everything in `state.db` is state. Classifying first is what makes
both the migration and the sync rules small — **and note that each class
gets a different conflict rule in §5, so this table is not bookkeeping,
it is the sync design.**

### CONFIGURATION → files under Git (never synced by us — git is the sync)

| Table | Why it is configuration |
|---|---|
| `node_comms_policy` | A projection of `spec.yaml`'s `metadata.labels` / `spec.comms` / `spec.lineage`. Both writers derive it from the spec. |
| `comms_grants` | Operator-declared cross-group permissions. |
| `comms_blocks` | Operator-declared receiver-side vetoes. |
| `definitions` | Content-addressed cache of git-resident YAML. |

### STATE → PostgreSQL, single-writer per row

`instances`, `comms_nodes`, `lineage`, `a2a_ports`, `node_tokens`
(secret — §7), `inbound_dispatches`, `pending_prompts`,
`acl_deny_notify_log`, `agent_residency`, `relocation_leases`.

Almost all of these are **facts about a host, authored by that host**.
That is what makes them safely syncable (§5).

### LOG / EVENT → PostgreSQL, append-only

`events`, `attempts`, `turns`, `errors`, `heartbeats`,
`instance_heartbeats`, `channel_events`, `dispatches`,
`relocation_journal`, `verdict_delivered`. Bulk of the bytes, least
urgent, and the easiest to sync (pure union).

---

## 4. The primitive

### Where it lives

`scitex_dev.state`, a new subpackage of **scitex-dev**. That is the
right home because scitex-dev *already owns the node registry*:
`scitex_dev.hosts` provides `HostRecord` / `list_hosts()` /
`resolve(name)` over `~/.scitex/dev/hosts.yaml`, with a write-guard
(`resolve_hosts_yaml_for_write`) that already refuses when several
candidate files are visible — the container-shadow trap, solved.

It must **not** be built on `scitex-db`: measured, that package is
psycopg2-based, is not installed in `/opt/venv-sac`, pulls `scitex-core`,
and has no pooling, no upsert/`ON CONFLICT`, and no multi-host notion.
`psycopg` 3.3.4 is already present, and scitex-cards' psycopg3 layer is
proven in production — copy that driver layer.

### API

```python
from scitex_dev.state import connect, dsn_for, register_node, store_identity

with connect(dsn_for()) as conn:        # this node's own :55432
    with write_transaction(conn):       # pg_advisory_xact_lock(<fixed int64>)
        conn.execute("INSERT INTO instances ... ON CONFLICT ...")
```

* `dsn_for(node=None) -> str` — resolves from `hosts.yaml`. **Never
  returns a `Path`**; raises `StoreTargetNotConfigured` rather than
  guessing (servers must not guess). Never emits `5432`.
* Password **never** in the DSN — libpq reads `$PGPASSFILE`. Both reach
  a container through the spec's `--env` list.
* Schema asserted **once per store**, not once per `connect`. sac's
  current `init_schema`-on-every-`open_db` would become a per-open
  network DDL storm; scitex-cards already hit exactly that.
* `store_identity(conn) -> (store_uuid, system_identifier)` — the pair,
  because `store_uuid` alone provably cannot detect a clone (§2).

### How a host registers

`HostRecord` gains a `pg` block in `~/.scitex/dev/hosts.yaml`:

```yaml
hosts:
  - name: scitex-compute-04
    kind: compute
    pg: {port: 55432, node_id: compute-04, sync_peers: [ywata-note-win]}
```

`node_id` is the stable identity stamped into every row this host
authors (§5). It must never be reused or renamed — it is the partition
key that makes single-writer ownership work.

---

## 5. Synchronisation — the answers Wave-lead asked for

### 5.1 Is sync part of this primitive, or layered on it?

**Layered — a separate component — but it cannot be added later unless
the state primitive mandates a schema contract NOW.**

Separate, because sync has its own failure modes (partial, resumable,
auditable) and different packages will want different cadences and
per-table policies; and because a node must work fully while isolated,
so nothing on the write path may depend on sync being reachable.

Joined, because sync is impossible to retrofit onto rows that lack
identity and provenance. **Every synced table MUST carry:**

| Column | Purpose |
|---|---|
| `origin_node TEXT NOT NULL` | which node authored the row — the ownership partition key |
| `row_uuid UUID NOT NULL` | globally unique row identity, minted at insert |
| `revision BIGINT NOT NULL DEFAULT 1` | monotonic, bumped by the owner on every update |
| `updated_at TIMESTAMPTZ NOT NULL` | reporting and ordering for humans — **never** the conflict rule |
| `deleted_at TIMESTAMPTZ NULL` | tombstone; rows are never `DELETE`d |

**A table created without these can never be synchronised without a
rewrite.** That is the one sentence every consumer stream needs tonight,
because tables are being created right now.

### 5.2 The conflict-resolution rule

Grounded in a measured incident: a one-time `pg_dump` replication
between two card stores diverged silently **in both directions**, and a
repair with `ON CONFLICT DO UPDATE` would have destroyed the other
side's newer work. `DO NOTHING` was the difference between repair and
data loss (card
`sac-card-store-split-brain-two-instances-two-databases-20260807`).

The rule is therefore **per class, and never a wall clock**:

1. **CONFIGURATION — not synced at all.** Git is the sync. This is the
   cheapest win available: it removes rows from the problem entirely,
   and it is what §6 implements.
2. **LOG / append-only — union; conflict impossible by construction.**
   The primary key includes `origin_node`, so two nodes can never author
   the same key. `ON CONFLICT DO NOTHING` is correct here precisely
   because a collision means the identical row arrived twice.
3. **STATE — single-writer, partitioned by `origin_node`.** A node may
   `UPDATE` only rows where `origin_node = <its own node_id>`. Every
   other node's rows are **read-only replicas**. Conflict is impossible
   by construction, not by arbitration. This works because almost all
   sac state is a fact about a host authored by that host.
4. **SHARED-MUTABLE (the hard case — scitex-cards `tasks`, which any
   agent on any host edits) — no automatic merge.** Accept a remote row
   only when it is a newer version *from that row's own owner*:
   `remote.origin_node == local.origin_node AND remote.revision >
   local.revision`. In every other case **DO NOTHING and write a
   divergence record** for operator adjudication. `tasks` already
   carries a `revision` column, so the hook exists.
5. **Deletes are tombstones**, never `DELETE` — consistent with "nothing
   is ever deleted".
6. **Fail loud, never guess.** A sync run that cannot decide reports and
   stops. Ambiguity resolves to DO NOTHING plus a report, which is
   exactly the posture that saved the data on 2026-08-07.

Blind last-write-wins on `updated_at` is **prohibited**: clocks skew
across hosts, and the measured incident was bidirectional, so either
direction of blind update loses work.

### 5.3 What happens when an agent relocates?

Nine agents are queued to move to compute-04, so this is concrete.

* **After the move the agent writes to its NEW host's `:55432`.** That
  is the point of per-host instances — local writes, no cross-host
  dependency at write time.
* **Rows it wrote on the old host stay on the old host**, tagged
  `origin_node = <old host>`. They are history; they are never rewritten
  in place and never deleted. Only the old host could rewrite them
  anyway, under rule 3.
* **What moves is residency, not rows.** `agent_residency` (already
  present) flips to the new host, and that flip is the authoritative
  statement of who may now author rows about the agent. **Exactly one
  current residency row per agent** — enforced, not assumed.
* **Ordering matters, and it is a trap for the nine queued moves.** The
  old host must close its `instances` row (`ended_at`) *before*
  residency flips. Flip first and the agent is simultaneously live on
  two hosts, with two `a2a_ports` claims — which is failure #2 of §1
  wearing a new hat.
* **Fleet-wide reads become a union over synced replicas**, not a query
  against one node. `sac agents list` answering identically everywhere
  is delivered by the *sync layer*, not by PostgreSQL itself. This is
  the crux: moving to PostgreSQL alone would not have fixed §1.

---

## 6. First slice, implemented

**`node_comms_policy` is configuration, and the fix is to stop caching
it — not to move the cache to PostgreSQL.** Under §5.2 rule 1 it then
never enters the sync problem at all.

Group membership is authored by a human in `spec.yaml`, read by a pure
function (`config/_group_resolver.py`), and changed by nobody at
runtime. Persisting it into a per-host table and then trusting that
table is the whole of failure #1 — and a per-host *PostgreSQL* table
would have reproduced it exactly, because per-host is the intended
topology.

So `resolve_group_names` / `resolve_group_name` now read the **spec**,
falling back to the persisted row only for nodes with no visible spec
(remote / federated peers). New module: `config/_group_authority.py`.

Four properties make it safe:

* **Tri-state.** `None` = "no spec visible from this process" (fall
  back); `frozenset()` / `""` = "the spec answered, and the answer is
  none". Collapsing those two is the original sin — it is what let a
  missing row read as a legitimate "ungrouped" and deny an agent that
  held authority.
* **Spec replaces the row, never unions with it**, so deleting a group
  from a spec actually revokes it. Sound because the column has no
  non-spec writer.
* **Never raises into an ACL path.** A missing / malformed / non-mapping
  spec yields `None` and falls back; it never fabricates a grant.
* **Fleet scope only — never project-local.** The general spec resolver
  searches `<repo>/.scitex/agent-container/agents/` *first*, which is
  right for `sac agents start` and dangerous for a permission check: an
  agent's workdir is a repository it edits, so a project-local spec
  would put self-elevation one `git add` away. Authority reads only
  `$SCITEX_AGENT_CONTAINER_YAML_DIRS` (operator-controlled, and already
  injected into every container) and the user-scope agents dir.

### The reproduction, and the fix, measured on this container

```
store consulted: /state/scitex-agent-container/state.db

BEFORE  resolve_group_names('scitex-agent-container') -> []
        resolve_group_names('grant')                  -> []

AFTER   resolve_group_names('scitex-agent-container') -> [active, developer, infra]
        resolve_group_names('grant')  -> [active, developer, generalist,
                                          privileged, researcher]
```

Every agent resolved to *no groups at all* from inside the container,
including itself, while `spec.yaml` on the same filesystem — resolvable
in the same process — said `groups: [developer, infra, active]`.
`host_exec`'s `ELIGIBLE_GROUPS` is `{developer, researcher, privileged}`,
so the spec said ALLOW and the cache said DENY for the same agent at the
same moment on the same machine. That is the 403.

`tests/scitex_agent_container/config/test__group_authority.py` builds two
**real** SQLite stores — a populated one (bare host) and an empty one
(the SIF's private `/state/<agent>/state.db` shard) — asserts they still
genuinely differ as databases, then asserts the group set, the primary
group, and `is_developer` are **identical** from both. No mocks; the
spec path is exercised through the real
`$SCITEX_AGENT_CONTAINER_YAML_DIRS` port containers already use.

---

## 7. Deferred — explicitly not done

1. **No STATE table has moved to PostgreSQL.** The primitive is
   specified here, not implemented.
2. **`scitex_dev.state` is not written**, and neither is the sync
   engine. No code exists in scitex-dev.
3. **The sync schema contract (§5.1) is not enforced anywhere** — no
   linter rule, no base-table helper. Until it exists, new tables will
   keep being created unsyncable.
4. **The four-way scitex-cards divergence is measured, not repaired.**
   Repair needs the sync engine plus an operator call on adjudication.
5. **The in-container `:55432` vs `:5442` disagreement is unfixed** —
   reported here; it is a live defect distinct from expected cross-host
   isolation.
6. **`hosts.yaml` `pg:` block is not implemented**, and `hosts.yaml` is
   not itself under git. "Configuration → files" is done for specs;
   "→ **under Git**" remains a separate step for both.
7. **`comms_grants` / `comms_blocks` / `definitions`** are classified as
   configuration but still live in SQLite.
8. **`node_tokens` is a secret** and must never follow
   `node_comms_policy` into git; its migration needs a credential story
   first.
9. **`~/.scitex/agent-container/runtime/state.db` on ywata-note-win is
   untouched**, deliberately: nine remaining relocations read it.
10. **Six of the eight named consumers are unsurveyed.**
