# ADR-0022 — States → PostgreSQL, Configuration → files under Git

* **Status**: Proposed (first slice implemented)
* **Date**: 2026-08-12
* **Operator rulings**: 「state.db というものは使ってはいけません」/
  「sqlite 使った瞬間負けだと思った方が良いです」/
  「今 5432 を scitex のために使ってるものはすべて間違い」/
  「今他のホストと連絡が取れてないっていうのはまぁ正常です。正しいです」/
  2026-08-11, and called universal
  (「これはどんな時も従うべき話だと思います」):
  「スペックというのは今動いてるエージェントの状態を表すのではなくて、未来に
  動くエージェントの規約」/「その状態に関しては必ずデータベースに入れなきゃ
  いけないし、その起動されたタイミングのスペックというのは、エージェントの中に
  焼き込まれないといけない情報です」/「スペックは設計書、実際に動いてる
  エージェントの状況はデータベース」 — recorded in §3 as a fourth class of
  fact, and as a sac skill
  (`_skills/scitex-agent-container/34_spec-is-a-contract-not-state.md`).
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

## 3. The cut: four kinds of fact, four homes

Not everything in `state.db` is state. Classifying first is what makes
both the migration and the sync rules small — **and note that each class
gets a different conflict rule in §5, so this table is not bookkeeping,
it is the sync design.**

**The classifying question is about TENSE, not about file format.** A
spec is written in the future tense: it is **the contract for an agent
that has not started yet**, and it stays a contract for the whole life of
the process it launched. It is never a description of a running agent.
The three consequences, per the operator's 2026-08-11 ruling:

1. **Design document → git.** The spec says what an agent *shall* be.
2. **What is actually true of a running agent → the database** (per-host
   PostgreSQL on `:55432`, §2).
3. **What the contract said AT LAUNCH → burned into the agent as a
   file**, so the agent can answer "how was I actually born" without
   asking a store, and without being told a later edit's answer.

Piece 3 is a distinct class of fact, not a copy of piece 1: the git-side
spec is **live and mutable**, and editing it does not retroactively
change how a running agent was started.

### CONFIGURATION → files under Git (never synced by us — git is the sync)

| Table | Why it is configuration |
|---|---|
| `node_comms_policy` | A projection of `spec.yaml`'s `metadata.labels` / `spec.comms` / `spec.lineage`. Both writers derive it from the spec. |
| `comms_grants` | Operator-declared cross-group permissions. |
| `comms_blocks` | Operator-declared receiver-side vetoes. |

`definitions` was listed here — "content-addressed cache of git-resident
YAML" — until 2026-08-28, when it was DELETED rather than migrated. The
classification was right and it was the classification that condemned it:
a cache of something git already holds is only worth carrying if somebody
fills it, and no code path has ever INSERTed a row (0 rows on every
state.db measured; `_store_plugin.NEVER_SYNCED` had recorded the finding
before this ADR was written). `instances.definition_id` keeps its
all-NULL column; only the table and the `REFERENCES` clause are gone.

### STATE → PostgreSQL, single-writer per row

`instances`, `comms_nodes`, `lineage`, `a2a_ports`,
`inbound_dispatches`, `pending_prompts`, `acl_deny_notify_log`,
`agent_residency`, `relocation_leases`.

`node_tokens` was listed here as state (secret — §7) until 2026-08-28,
when it was DELETED rather than migrated. See open question 8.

Almost all of these are **facts about a host, authored by that host**.
That is what makes them safely syncable (§5).

### LOG / EVENT → PostgreSQL, append-only

`turns`, `errors`, `heartbeats`, `channel_events`, `dispatches`,
`relocation_journal`, `verdict_delivered`. Bulk of the bytes, least
urgent, and the easiest to sync (pure union).

`events`, `attempts` and `instance_heartbeats` were listed here too, and
all three were DELETED rather than migrated — `attempts` on 2026-08-28,
`events` and `instance_heartbeats` the same day. This paragraph is the
amendment rather than a footnote because the list above was, until it was
written, the document telling a reader to MIGRATE tables the evidence says
to drop:

* `instance_heartbeats` — its writer (`update_heartbeat`) and its reader
  (`latest_instance_heartbeat`) each had ZERO callers in `src/`, and it
  held 0 rows on compute-01, compute-03, compute-04 and nas-03. Migrating
  it would have carried an empty table onto a new backend and kept the
  determinism argument in its DDL comment alive for another year.
* `attempts` — never had a writer at all.
* `events` — the one that is NOT empty (1181 rows on the host state.db)
  and still should not move, because it has zero READERS. Both its writers
  wrote `kind='start'` / `'stop'` as SQL literals, and both facts are
  already on the `instances` row in the same transaction, which is the
  same argument `_store_plugin.NEVER_SYNCED` gives for refusing to
  replicate it. It was also never a faithful log: `state_db_gc` closes
  stale instances with a bare UPDATE and wrote no event, so GC-reaped
  deaths were already missing from it.

"Append-only log" is a category that earns a migration only when something
reads the log. These three did not, and the existing rows stay on disk —
nothing is dropped, sac just stops issuing the DDL and stops claiming to
maintain them.

### LAUNCH SNAPSHOT → a file burned into the agent

The spec **as it stood at the moment of launch**, frozen and carried
inside the agent. Not synced, not mutable, not authoritative about the
present — it answers exactly one question, and only the agent asks it:
*how was I actually born?*

It is needed because neither of the other homes can answer it:

* The **git-side spec is live**. Measured on this container: the bound
  spec dir is `…/agents/scitex-agent-container/`, the same directory the
  operator edits, and it holds `.old/20260812T190500Z/` and
  `.old/20260812T222000Z/` — two rewrites of this agent's own spec in one
  evening. Reading it answers "what would I be if started NOW".
* The **database holds the present**, not the terms of a past launch.

Deliberately a file, not a row: it must remain readable by an agent whose
store is unreachable, and it must be immune to the class of bug in §1
(an answer that changes with where the reader stands).

### The failure this forbids: reading a promise as a fact

A spec field may legitimately declare a **promise to resolve later**.
Reading such a field as though it were a value yields a sentinel, and a
sentinel silently fails every numeric or identity test applied to it.

Measured on scitex-compute-04, 2026-08-11 — `spec.a2a.port` across every
`agents/*/spec.yaml` (107 files, of which 3 are `_template_*` scaffolds,
so **104 real agent specs**):

| what the spec declares | agents |
|---|---|
| `port: auto` | 93 |
| `port: null` (sidecar deliberately off) | 11 |
| **a concrete int** | **0** |

The near-miss, same night: the tui turn-bridge supervisor (PR #973) must
know which port an agent's bridge should serve. Had it read
`config.a2a.port`, it would have received the literal string `"auto"`,
found no number, concluded there was nothing to supervise, and supervised
**nothing — on every agent in the fleet**, while reporting healthy. It
reads the port allocator's **claim** instead (`a2a_ports`, which is
state): `sac agents list scitex-agent-container` answers `19016`, a fact
that exists only because a start already happened.

`session_id` is the same split and is not yet resolved: what a spec
declares (`session: continue` / `fresh`) is a promise about how to
resume; *which conversation actually resumed* is state. Card
`sac-pin-session-id-at-start-removes-f34-20260812` tracks it.

**The rule, stated so it can be applied without this ADR in hand:** if
answering a question requires knowing that a start has already happened,
the spec cannot answer it. Ask the database.

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
7. **`comms_grants` / `comms_blocks`** are classified as configuration but
   still live in SQLite. `definitions` was named here too until
   2026-08-28; it no longer lives anywhere. It was deleted rather than
   migrated — nothing had ever written it — so this line is one open
   question shorter rather than one answer longer.
8. ~~**`node_tokens` is a secret** and must never follow
   `node_comms_policy` into git; its migration needs a credential story
   first.~~ **RESOLVED 2026-08-28 — the credential story is that there
   was no credential.** The table was measured before deciding: 0 rows
   on compute-01, compute-03, compute-04 and nas-03 (`runtime/state.db`,
   2026-08-28 11:40Z), and `mint_node_token` had ZERO callers outside
   the test suite. So `resolve_node_token` always returned `None`,
   `request.state.authenticated_node` was always `None`, and the
   per-node anti-spoofing branch in `check_send_acl` had never fired.
   The feature was never armed; it was removed rather than migrated,
   along with the table, the DDL, the middleware and the `KNOWN_TABLES`
   entry. What gates a send is the host-wide bearer plus the name-based
   ACL, which is what gated it all along.

   Removal also closed an export hole by construction: `export_state`
   ships every column of a `KNOWN_TABLES` member — `token` included —
   and the MCP `db_export` tool takes no `tables` argument with which
   to withhold one. The table's absence is now the guarantee that used
   to depend on nobody calling export.

   `_store_plugin.NEVER_SYNCED` deliberately KEEPS its refusal of the
   name: a table leaving `KNOWN_TABLES` must not read as the refusal
   being withdrawn, and that entry is where a future per-node
   credential store would arrive. If one is ever built, this open
   question re-opens with it.
9. **`~/.scitex/agent-container/runtime/state.db` on ywata-note-win is
   untouched**, deliberately: nine remaining relocations read it.
10. **Six of the eight named consumers are unsurveyed.**
11. **The launch snapshot (§3) is specified, not implemented.** No file
    is burned into an agent today. What a container can read is the LIVE
    spec dir, bind-mounted at `$SCITEX_AGENT_CONTAINER_YAML_DIRS` — so an
    agent asking "how was I born" currently gets "how you would be born
    if started now". Design constraints when it is built: written once at
    start, never rewritten, readable with the store unreachable, and
    carrying the RESOLVED values (the claimed port, not `auto`) beside
    the declared ones.
12. **`session_id` is not pinned at start.** `sac agents list <name>` can
    report `session_id: null` for an agent with a live session — the
    promise is recorded, the fact is not. Card
    `sac-pin-session-id-at-start-removes-f34-20260812`.
13. **The mechanical check is deliberately narrow, and the general rule
    is NOT mechanised.** `STX-SAC004` (this package's linter plugin,
    severity *warning*) fires only when a listed sentinel-bearing spec
    field is `return`ed or passed as a call argument without the
    enclosing function narrowing the sentinel; `_SENTINEL_FIELDS` holds
    exactly one entry (`a2a.port`). Comparisons are not flagged —
    asserting what a contract says is the correct way to read one.
    "This code is reading a spec to learn a running agent's state" is not
    mechanically decidable, and a rule that fired on every `config.`
    access would be noise; the fleet has a precedent for what happens
    then (a rule shipped at error severity turned 44 repositories red on
    day one and was restaged to warning the next day). A new
    resolve-at-runtime field is invisible to the rule until it is added
    to that list.
14. **The spec header is emitted by the GENERATORS only.** Every spec
    scaffolded by `sac agents create` (minimal + full) and by the
    contributor renderer now opens with two lines — "this is a design
    document / a running agent's state lives in the database". The 113
    specs already on disk are NOT swept by this change, the operator's
    `_template_*/` dir-templates live outside this repo, and nothing
    enforces the header's presence: a YAML comment is not schema, and
    making it one would turn a note for humans into a validator's
    business. Accepted — its audience is a person with the file open.
    The three `_template_*/` dir-templates on scitex-compute-04 were
    given the header by hand (backups under
    `~/.scitex/agent-container/.old/20260811T235819Z/dir-templates/` with
    a manifest); the remaining 104 are carded as
    `sac-sweep-design-doc-header-into-existing-specs-20260812`.
