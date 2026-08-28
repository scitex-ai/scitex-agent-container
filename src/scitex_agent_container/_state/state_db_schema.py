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
anywhere; it simply never had a writer.
"""

from __future__ import annotations

# Registry tables (F-CS11) — definitions, instances, events.
# The legacy ``heartbeats`` table (instance_id, ts, ...) is now
# created under the name ``instance_heartbeats``. Nothing in this file
# competes for the bare name any more: the diary-style ``heartbeats``
# left SQLite on 2026-08-28 (see :mod:`.state_db_diary`), so the rename
# migration in :func:`.state_db_migrations.migrate_legacy_heartbeats`
# is now the ONLY thing that ever writes that name here.
_SCHEMA_REGISTRY = """
CREATE TABLE IF NOT EXISTS definitions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    yaml_path       TEXT NOT NULL,
    yaml_sha256     TEXT NOT NULL,
    scope           TEXT NOT NULL,
    runtime         TEXT,
    first_seen_at   TEXT NOT NULL,
    UNIQUE(yaml_path, yaml_sha256)
);

CREATE TABLE IF NOT EXISTS instances (
    id                  TEXT PRIMARY KEY,
    definition_id       TEXT REFERENCES definitions(id),
    name                TEXT NOT NULL,
    host                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    pid                 INTEGER,
    ppid                INTEGER,
    screen              TEXT,
    workdir             TEXT,
    a2a_port            INTEGER,
    started_at          TEXT NOT NULL,
    last_heartbeat_at   TEXT,
    ended_at            TEXT,
    exit_reason         TEXT,
    iter_count          INTEGER DEFAULT 0,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    -- Family-tree / cross-host columns (sac-agent-spawn design, Rule
    -- B/D). ``bound_port`` mirrors ``a2a_port`` for new readers (both
    -- written together so legacy ``a2a_port`` callers keep working);
    -- ``remote`` is 1 for a cross-host-dispatched agent; ``spawned_by``
    -- is the launching identity ("cli"/parent-agent-name) — the lineage
    -- edge the spawn DAG is reconstructed from.
    bound_port          INTEGER,
    remote              INTEGER DEFAULT 0,
    spawned_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_instances_active
    ON instances(name, host, scope) WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_instances_host
    ON instances(host);

-- ``seq`` (AUTOINCREMENT) gives a total insertion order so "latest
-- heartbeat" is MAX(seq) — deterministic regardless of ``ts``
-- (second-resolution) ties. ``UNIQUE(instance_id, ts)`` keeps the
-- same-second collapse via the ON CONFLICT upsert in update_heartbeat.
-- See state_db_heartbeats / state_db_migrations for the full rationale.
CREATE TABLE IF NOT EXISTS instance_heartbeats (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL REFERENCES instances(id),
    ts              TEXT NOT NULL,
    iter            INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    pane_state      TEXT,
    UNIQUE (instance_id, ts)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    instance_id     TEXT,
    definition_id   TEXT,
    kind            TEXT NOT NULL,
    actor           TEXT,
    payload_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_instance
    ON events(instance_id, ts);
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

# Channel-event durability (WI-1) + the WI-2 / ADR-0014 ACL tables.
#
# THIS CONSTANT WAS ``_SCHEMA_DIARY`` UNTIL 2026-08-28. It was named for
# the three tables it opened with — ``turns``, ``errors``, ``heartbeats``
# — and those moved to per-host PostgreSQL that day (:mod:`.state_db_diary`
# holds both the writers and the reader now). The DDL went with them, and
# so did the name: a constant still called ``_SCHEMA_DIARY`` while
# defining only channel and ACL tables is a lie no grep can see through,
# and the next reader asking "where does the diary live" would land here
# and find ``channel_events``.
#
# The DDL is REMOVED rather than left behind, for the reason
# ``incarnations`` was removed from ``KNOWN_TABLES`` on 2026-08-19: a
# SQLite table that exists and is never written returns an EMPTY result
# to every reader, and an empty result reads as "this agent recorded no
# turns" when the truth is "you are asking the wrong database".
_SCHEMA_CHANNEL_AND_ACL = """
-- WI-1 channel-event durability (handoff §4 "Durability /
-- replay-on-reconnect"): persist every channel-bus event so a POST
-- with no subscriber is delivered on connect, and a kill+reconnect
-- replays exactly the missed events.
--
-- ``id`` is the SSE-cursor (the value of the SSE ``id:`` line); a
-- reconnecting client passes it back as ``Last-Event-ID`` to resume
-- without dropping or duplicating events.
-- ``meta_json`` carries the full minted envelope so the inbox bus can
-- replay byte-identical frames after a process restart.
-- ``delivered_at`` is set the first time the event reaches a live
-- subscriber; NULL means "still waiting on the bus".
CREATE TABLE IF NOT EXISTS channel_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target        TEXT NOT NULL,
    source        TEXT,
    kind          TEXT NOT NULL DEFAULT 'message',
    content       TEXT,
    meta_json     TEXT NOT NULL,
    ts            REAL NOT NULL,
    delivered_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_channel_events_target_undelivered
    ON channel_events(target, id) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_channel_events_target_id
    ON channel_events(target, id);

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

CREATE TABLE IF NOT EXISTS lineage (
    child_name   TEXT PRIMARY KEY,
    parent_name  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_parent ON lineage(parent_name);

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
