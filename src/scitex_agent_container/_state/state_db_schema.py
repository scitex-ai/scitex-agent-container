"""SQL DDL string constants for state.db.

Extracted verbatim from :mod:`state_db` (which grew past the 512-line
module cap). These are pure ``CREATE TABLE`` / ``CREATE INDEX`` scripts
run via ``conn.executescript`` in ``state_db.init_schema``; keeping them
in a focused sibling mirrors the existing ``state_db_*`` split
convention (state_db_export / state_db_gc / state_db_instances / ...).

``state_db`` re-imports both names, so every existing
``from ...state_db import _SCHEMA_*`` / ``executescript(_SCHEMA_*)`` call
site is unchanged.

WHAT IS NO LONGER HERE: the diary trio (``turns`` / ``errors`` /
``heartbeats``). They moved to per-host PostgreSQL on 2026-08-28 and
:mod:`.state_db_diary` owns them end to end — writers, reader, schema.
Also gone, same day: ``attempts`` and its ``_SCHEMA_ATTEMPTS`` constant —
see the departure note below the registry block. That one did not move
anywhere; it simply never had a writer. Same day again, and for the same
kind of reason rather than a move: ``definitions``, ``instance_heartbeats``
and ``events`` — see the three departure notes inside the registry block.

WHAT IS STILL HERE, after all of that: ``instances`` and the WI-1
durability table ``channel_events``. The WI-2 spawn DAG ``lineage`` was
the third until 2026-08-28, when it left for the shared PostgreSQL store
— see its departure note below. Two tables, and each of them has a live
writer AND a live reader.
"""

from __future__ import annotations

# Registry tables (F-CS11) — NONE. The block below is departure notes only.
#
# It was ``definitions``, ``instances``, ``events`` plus the renamed
# ``instance_heartbeats``. All four left on 2026-08-28: three because
# nothing read them, and ``instances`` — the last one standing, and the
# only one of the four that both a writer and a reader ever reached —
# because it moved to the SHARED PostgreSQL store
# (:mod:`.state_db_instances`).
#
# A ``_SCHEMA_REGISTRY`` that creates no table is kept rather than deleted
# because the notes ARE the deliverable: each one records what left, what
# it cost, and why the empty table would have been worse than none.
_SCHEMA_REGISTRY = """
-- ``definitions`` (id / name / yaml_path / yaml_sha256 / scope / runtime /
-- first_seen_at, UNIQUE(yaml_path, yaml_sha256)) was defined here until
-- 2026-08-28. It was the content-addressed cache of a spec's YAML, and no
-- code path has ever INSERTed a row into it: 0 rows on every state.db
-- measured, and ``_store_plugin.NEVER_SYNCED`` had already written the
-- finding down — "in KNOWN_TABLES, FK'd from instances.definition_id, and
-- never INSERTed by any code path".
--
-- REMOVED rather than left behind, under the ``attempts`` ruling recorded
-- below the registry block: a CREATE TABLE with no writer is WORSE than
-- no table, because the empty table answers every generic reader with a
-- plausible zero. For this one the plausible zero had a specific reading
-- available to it — ``sac db show`` printing ``definitions 0`` next to
-- ``instances 604`` says "no agent specs are registered", which is a
-- claim about the fleet rather than about the schema.
--
-- THE FK IS GONE FROM ``instances`` BELOW, THE COLUMN IS NOT.
-- ``definition_id`` keeps its data (all of it NULL, measured); only the
-- ``REFERENCES definitions(id)`` clause is dropped, because
-- ``state_db._connect`` runs ``PRAGMA foreign_keys = ON`` and a FK naming
-- a table SQLite no longer has makes every INSERT into ``instances``
-- raise. Dropping the column is a separate change with a separate
-- argument, and it is not this one.

-- ``instances`` (id / definition_id / name / host / scope / pid / ppid /
-- screen / workdir / a2a_port / started_at / last_heartbeat_at / ended_at /
-- exit_reason / iter_count / input_tokens / output_tokens / bound_port /
-- remote / spawned_by, plus idx_instances_active and idx_instances_host)
-- was defined here until 2026-08-28. It was the LARGEST table in this file
-- — 603 rows on compute-04, nine caller modules — and unlike the three that
-- left beside it, it left because it MOVED: :mod:`.state_db_instances` now
-- reads and writes it in the shared PostgreSQL store.
--
-- FOUR COLUMNS DID NOT MAKE THE TRIP, and each is named here rather than
-- quietly dropped, because each is a real (accepted) change:
--   ``definition_id``  a FK to ``definitions`` — a table nothing ever
--                      INSERTed into, and now gone from this file too.
--                      NULL on every row ever written.
--   ``scope``          written as the literal 'global' by both writers and
--                      read by nobody; it lived only inside
--                      idx_instances_active(name, host, scope).
--   ``ppid``           a parameter with no call site. NULL on every row.
--   ``bound_port``     FOLDED into ``a2a_port`` rather than dropped. Both
--                      columns always carried ONE value written twice, and
--                      the split is what let two routing readers answer
--                      "where do I send this" differently from the same
--                      row. The store keeps one port and mirrors both KEYS
--                      back out, so the readers cannot diverge again.
--
-- Deleting this DDL drops NO existing rows: ``CREATE TABLE IF NOT EXISTS``
-- simply stops being issued, so an old state.db keeps whatever it holds and
-- ``scripts/migrate_instances_to_postgres.py`` is what carries it across.

-- ``events`` (id AUTOINCREMENT / ts / instance_id / definition_id / kind /
-- actor / payload_json, plus idx_events_instance) was defined here until
-- 2026-08-28. Unlike the two above it was not empty — 1181 rows on the
-- host state.db, 140 in a container shard — and it left anyway, because
-- it has ZERO READERS. No ``SELECT ... FROM events`` exists in ``src/``;
-- only the generic KNOWN_TABLES consumers reached it.
--
-- Its two writers were both in :mod:`.state_db_instances`
-- (``record_instance_start`` / ``record_instance_stop``) and they wrote
-- ``kind='start'`` / ``'stop'`` as SQL LITERALS with ``actor='sac'``, so
-- the kind set could not widen from a caller. Every fact they recorded is
-- written to the ``instances`` row in the SAME transaction: ``started_at``,
-- ``ended_at``, and ``payload_json``'s only content ``exit_reason``.
--
-- And it was already not a faithful log, which is the reading that decides
-- it. ``state_db_gc`` closes stale instances with a bare UPDATE and writes
-- no event, so every GC-reaped death was ALREADY missing from this table.
-- A lifecycle log that silently omits one class of death is worse than no
-- log: an absence in it reads as "that did not happen".
--
-- Deleting this DDL drops NO existing rows. ``CREATE TABLE IF NOT EXISTS``
-- simply stops being issued, so the 1181 rows on an old state.db stay
-- exactly where they are, queryable with ``sqlite3`` by anyone who wants
-- the history. What stops is sac claiming to maintain it.
"""

# ``attempts`` (and its two indexes) was defined here as
# ``_SCHEMA_ATTEMPTS`` until 2026-08-28. It predated state.db — it lived in
# ``actions.db`` and was bundled in so state.db was self-contained on a
# fresh host — and by the time it landed here nothing wrote it: ZERO
# INSERTs anywhere in ``src/``, only tests. Unlike the tables that left
# before it, it did not move to PostgreSQL; there was no history to carry,
# because none was ever recorded.
#
# REMOVED rather than left behind, under the same ruling as ``comms_grants``
# and ``node_comms_policy`` below: a CREATE TABLE with no writer is WORSE
# than no table. The empty table answers every reader with a plausible-
# looking zero — ``sac db show`` prints ``attempts 0``, ``sac db query
# --table=attempts`` prints nothing, ``sac db export`` ships an empty array
# — and a zero meaning "nobody ever wrote this" is indistinguishable from a
# zero meaning "this agent did nothing". No table at all raises, which is
# the honest answer.
#
# Deleting this DDL drops NO existing rows: ``CREATE TABLE IF NOT EXISTS``
# simply stops being issued, so an old state.db keeps whatever it holds.

# The WI-2 / ADR-0014 ACL tables.
#
# THIS CONSTANT WAS ``_SCHEMA_DIARY`` UNTIL 2026-08-28, then
# ``_SCHEMA_CHANNEL_AND_ACL`` for the rest of that day. Each rename tracked
# a table leaving: first the diary trio (``turns`` / ``errors`` /
# ``heartbeats``) to per-host PostgreSQL, then ``channel_events`` to the
# shared one. A constant named for tables it no longer defines is a lie no
# grep can see through, and the next reader asking "where does the channel
# history live" would land here and find nothing to tell them.
#
# ``channel_events`` LEFT ON 2026-08-28, the LAST SQLite table sac owned.
# It is now two plain PostgreSQL tables — ``sac_channel_events`` and
# ``sac_channel_cursor`` — in the shared database ``host_store`` resolves
# to; :mod:`.state_db_channel_store` holds their DDL and
# ``docs/adr/0023-channel-events-plain-postgres.md`` holds the three
# measurements that kept them OUT of ``scitex_dev.store``.
#
# The DDL is REMOVED rather than left behind, for the reason
# ``incarnations`` was removed from ``KNOWN_TABLES`` on 2026-08-19: a
# SQLite table that exists and is never written returns an EMPTY result
# to every reader, and an empty result reads as "this agent has no waiting
# messages" when the truth is "you are asking the wrong database" — which,
# for the channel, means an inbox that looks delivered.
#
# Deleting this DDL drops NO existing rows: ``CREATE TABLE IF NOT EXISTS``
# simply stops being issued, so an old state.db keeps whatever it holds
# until ``scripts/migrate_channel_events_to_postgres.py`` carries it over.
_SCHEMA_ACL = """
-- WI-2 ACL — authenticated identity, lineage edges, cross-group grants
-- (handoff §4; lead 2026-05-21 RESTORED the authenticated-identity
-- criterion the prior limited scope had deferred).
--
-- ``lineage`` records parent → child edges produced by
-- ``sac agents start``. A node's *group* (the default-ACL unit) is
-- derived from lineage: parent + parent's direct children. Schema
-- stays N-level capable — see derive_group() for the traversal.
--
-- ``comms_grants`` was defined here until 2026-08-28. Its readers had
-- already moved to the shared PostgreSQL store via
-- :mod:`.state_db_grants`, which resolves through ``host_store`` and
-- carries no SQLite path at all, so this DDL was creating a table
-- nothing read or wrote. A CREATE TABLE with no writer leaves an empty
-- table that answers "no grants" instead of raising, which is the
-- reading that turns a migration gap into a silent deny.
--
-- ``node_tokens`` (and ``idx_node_tokens_token``) was defined here until
-- 2026-08-28. It was the WI-2 authenticated-identity primitive: a bearer
-- token per node, which ``_listen`` resolved back to a name so
-- ``check_send_acl`` could refuse a ``metadata.from_agent`` that did not
-- match it. ``mint_node_token`` had ZERO callers outside tests, so the
-- table held 0 rows on compute-01/-03/-04 and nas-03, every resolve
-- returned None, and the anti-spoof branch never fired once.
--
-- The empty table was not itself unsafe — it answered "no such bearer"
-- correctly, which is the SAFE answer. The hazard was the DECLARATION:
-- a schema naming a per-node credential store promises identity that
-- cannot be forged through a metadata field, and every reader of this
-- file (and of ``NEVER_SYNCED``, which still refuses the name) inherited
-- that promise while the serving code could not honour it. Removing the
-- table is what makes the file agree with the fleet: the host-wide
-- bearer and the name-based ACL are the gate, and there is no second,
-- stronger identity waiting behind them.
--
-- Removal also closes an export hole by construction: ``export_state``
-- ships every column of a KNOWN_TABLES member, the ``token`` column
-- included, and the MCP ``db_export`` tool exposes no ``tables``
-- parameter with which to hold it back.

-- ``lineage`` (and ``idx_lineage_parent``) was defined here until
-- 2026-08-28. The spawn DAG moved to PostgreSQL via scitex_dev.store; its
-- schema is created on first open by
-- ``state_db_lineage_store.open_lineage_store``, so there is nothing to
-- create here.
--
-- REMOVED rather than left behind, and of the five tables that have left
-- this file this is the one where an empty leftover would have been
-- ACTIVELY DANGEROUS rather than merely wrong. Every reader of these edges
-- treats "no row for this child" as ROOT — and a root MAY SPAWN
-- (``spawn_allowed``). A CREATE TABLE with no writer therefore would not
-- have degraded the ACL, it would have INVERTED it: an empty table hands
-- every agent in the fleet the spawn authority the gate exists to
-- withhold, silently, with nothing logged and nothing 403ing. The same
-- emptiness also collapses ``derive_group`` to a singleton (isolating
-- agents that should mesh) and ``descendants_of`` to nothing (so
-- ``check_lineage_acl`` denies a parent authority over its own child).
-- No table at all is the honest answer: the reader raises rather than
-- answering "no parent" from the wrong database.

-- The ADR-0014 comms graph ``comms_nodes`` (and its ``host`` index) was
-- defined here until 2026-08-28. It moved to PostgreSQL via
-- scitex_dev.store; its schema is created on first open by
-- ``state_db_comms_nodes_store.open_comms_nodes_store``, so there is
-- nothing to create here.
--
-- REMOVED rather than left behind, under the same ruling as
-- ``comms_grants`` above and ``node_comms_policy`` below — and this one
-- is the table where an empty leftover would have been read as ROUTING
-- TRUTH. ``resolve_node_host`` falls through to this directory when no
-- live ``instances`` row matches, and answers ``None`` when it finds
-- nothing; every caller reads ``None`` as "not in the federated graph, do
-- not cross-host forward". A CREATE TABLE with no writer would therefore
-- have turned "you are asking the wrong database" into "that agent is
-- local", silently, on the forwarding path.
--
-- Two of the seven columns have no successor here and that is the point
-- of the move rather than a loss. ``ended_at`` was a hand-rolled soft
-- tombstone that existed so ``export_state`` could carry a deletion —
-- which ``import_state``'s INSERT OR IGNORE then dropped, as the old
-- module's own docstring admitted. The store's ``hide()`` IS the
-- tombstone, it replicates as an op, and nothing is hard-deleted, so the
-- withdrawal that could never propagate now propagates by construction.
-- ``source_host`` was provenance the writer had to remember to fill (NULL
-- on every locally registered row); the reserved ``_origin`` column is
-- stamped by the primitive on every op.

-- The Phase-3 ACL table ``node_comms_policy`` was defined here until
-- 2026-08-28. It moved to PostgreSQL via scitex_dev.store; its schema is
-- created on first open by ``state_db_acl_policy_store.open_policy_store``,
-- so there is nothing to create here.
--
-- REMOVED rather than left behind, which for an ACL matters more than for
-- the tables that went before it. A CREATE TABLE with no writer leaves an
-- EMPTY table, and an empty ACL table does not read as "wrong database" —
-- ``read_comms_policy`` answers a missing row with all-allow defaults, so a
-- stale reader querying the abandoned table would have been handed
-- PERMISSION, silently, with no error anywhere. No table at all is the
-- honest answer.
"""
