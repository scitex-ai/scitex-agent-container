"""SQL DDL string constants for state.db.

Extracted verbatim from :mod:`state_db` (which grew past the 512-line
module cap). These are pure ``CREATE TABLE`` / ``CREATE INDEX`` scripts
run via ``conn.executescript`` in ``state_db.init_schema``; keeping them
in a focused sibling mirrors the existing ``state_db_*`` split
convention (state_db_export / state_db_gc / state_db_diary / ...).

``state_db`` re-imports all three names, so every existing
``from ...state_db import _SCHEMA_*`` / ``executescript(_SCHEMA_*)`` call
site is unchanged.
"""

from __future__ import annotations

# Registry tables (F-CS11) — what is LEFT of them.
#
# ``instances`` and ``events`` were defined here until 2026-08-28. Both
# moved to per-host PostgreSQL (:mod:`.state_db_instances` and
# :mod:`.state_db_instance_events`) and the DDL was DELETED rather than
# left in place, which is the part that matters. A retired CREATE TABLE
# is not inert: it keeps handing a stale reader an empty table instead of
# an error, and for ``instances`` an empty table is not a null result —
# it is the sentence "no agents are running", which reads as an answer
# and gets acted on. Deleting the DDL turns that silent wrong answer into
# ``no such table: instances``, which nobody mistakes for a fleet status.
#
# The legacy ``heartbeats`` table (instance_id, ts, ...) is created under
# the name ``instance_heartbeats``. See _SCHEMA_DIARY below for the
# diary-style ``heartbeats``.
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

-- ``seq`` (AUTOINCREMENT) gives a total insertion order so "latest
-- heartbeat" is MAX(seq) — deterministic regardless of ``ts``
-- (second-resolution) ties. ``UNIQUE(instance_id, ts)`` keeps the
-- same-second collapse via the ON CONFLICT upsert in update_heartbeat.
-- See state_db_heartbeats / state_db_migrations for the full rationale.
--
-- ``instance_id`` lost its ``REFERENCES instances(id)`` clause on
-- 2026-08-28: the referenced table is in another engine now, and a
-- foreign key SQLite cannot check is a constraint in name only. The
-- column still holds the instance id and still means the same thing;
-- what is gone is the pretence that the database enforces it.
CREATE TABLE IF NOT EXISTS instance_heartbeats (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL,
    ts              TEXT NOT NULL,
    iter            INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    pane_state      TEXT,
    UNIQUE (instance_id, ts)
);
"""

# Attempts predates state.db (lived in actions.db). Bundled here so
# state.db is self-contained on a fresh host.
_SCHEMA_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    agent        TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    elapsed_s    REAL    NOT NULL,
    pane_before  TEXT,
    pane_after   TEXT,
    extras       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
CREATE INDEX IF NOT EXISTS idx_attempts_agent_action ON attempts(agent, action);
"""

# Diary tables (2026-05-17): turns / errors / heartbeats. Each
# agent appends rows like a journal; the lead reads + filters.
_SCHEMA_DIARY = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id        TEXT NOT NULL,
    name           TEXT NOT NULL,
    host           TEXT NOT NULL,
    status         TEXT NOT NULL,
    prompt_text    TEXT,
    response_text  TEXT,
    ts             REAL NOT NULL,
    session_id     TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_turn_id ON turns(turn_id);
CREATE INDEX IF NOT EXISTS idx_turns_name_ts ON turns(name, ts);

CREATE TABLE IF NOT EXISTS errors (
    error_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    host       TEXT NOT NULL,
    cause      TEXT NOT NULL,
    detail     TEXT,
    ts         REAL NOT NULL,
    turn_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_name_ts ON errors(name, ts);
CREATE INDEX IF NOT EXISTS idx_errors_cause ON errors(cause);

CREATE TABLE IF NOT EXISTS heartbeats (
    heartbeat_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    pid           INTEGER,
    state         TEXT NOT NULL,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_name_ts ON heartbeats(name, ts);

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
-- ``node_tokens`` is the authenticated-identity primitive. Each node
-- (sac-managed or external) gets a token minted at registration; the
-- listen server resolves an incoming ``Authorization: Bearer <token>``
-- to a node name via :class:`_listen._acl.NodeAuthMiddleware`. The
-- acceptance "identity cannot be spoofed via a metadata field"
-- (handoff §4) is enforced by ``check_send_acl``: when a per-node
-- bearer is presented, ``metadata.from_agent`` MUST match the bearer's
-- resolved name — a mismatch is a 403 with an explicit spoof reason.
--
-- ``lineage`` records parent → child edges produced by
-- ``sac agents start``. A node's *group* (the default-ACL unit) is
-- derived from lineage: parent + parent's direct children. Schema
-- stays N-level capable — see derive_group() for the traversal.
--
-- ``comms_grants`` records explicit cross-group send grants. A row
-- ``(sender, target)`` permits ``sender → target`` even when the
-- two are in different groups. With authenticated identity in force,
-- ``sender`` is the resolved-from-bearer name (administrative caller
-- path: the host-wide bearer honours ``metadata.from_agent`` verbatim
-- — used by cross-host forwarders authenticating with the
-- destination's host bearer pulled from ``peer-tokens/`` registry).
CREATE TABLE IF NOT EXISTS node_tokens (
    name        TEXT PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_tokens_token ON node_tokens(token);

CREATE TABLE IF NOT EXISTS lineage (
    child_name   TEXT PRIMARY KEY,
    parent_name  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_parent ON lineage(parent_name);

CREATE TABLE IF NOT EXISTS comms_grants (
    sender_name  TEXT NOT NULL,
    target_name  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    note         TEXT,  -- optional audit annotation
    PRIMARY KEY (sender_name, target_name)
);
CREATE INDEX IF NOT EXISTS idx_comms_grants_target ON comms_grants(target_name);

-- ADR-0014 — symmetric federated comms graph.
--
-- ``comms_nodes`` is the cross-host name → (host, a2a_port) directory
-- that resolves cross-host A2A targets. Every host writes locally;
-- ``sac registry sync`` ssh-pulls from peers and feeds ``import_state``
-- which idempotently merges rows (INSERT OR IGNORE on the ``name`` PK).
--
-- ``source_host`` is NULL for rows registered locally (operator
-- identity at listen startup, or agent-start hook). It is set to the
-- peer's canonical hostname when the row was pulled via
-- ``sac registry sync --from PEER`` — used by the conflict detector
-- in :func:`state_db_nodes.register_comms_node` to distinguish a
-- benign re-pull (same source) from a true name-collision (different
-- source claiming the same name with a different host/port).
--
-- ``ended_at`` is a soft tombstone — preserved on
-- :func:`unregister_comms_node` so the next ``export_state`` carries
-- the deletion to peers. A GC pass (not in Stage 1) will eventually
-- physically delete tombstoned rows older than a TTL.
CREATE TABLE IF NOT EXISTS comms_nodes (
    name           TEXT PRIMARY KEY,
    host           TEXT NOT NULL,
    a2a_port       INTEGER NOT NULL,
    registered_at  REAL NOT NULL,
    updated_at     REAL NOT NULL,
    source_host    TEXT,
    ended_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_comms_nodes_host ON comms_nodes(host);

-- Phase-3 ACL: per-spec capsule-isolation policy (ADR-0010 Step 2).
-- Row written at agent_start from the loaded spec.comms/spec.lineage
-- blocks. Read at ACL-check time by check_send_acl / check_spawn /
-- derive_group so policy lookups stay synchronous with no YAML re-parse.
-- Defaults match the dataclass defaults (everything "allow", may_spawn=1,
-- lineage_group=""), so absence of a row is byte-equivalent to the
-- pre-Phase-3 group-default ACL.
-- ``group_name`` (operator 2026-06-25): the agent's NAMED group, resolved
-- at agent_start from metadata.labels.group (else role-derived; the
-- developer-ish roles default to 'developer'). Read at ACL-check time so
-- a same-named-group send is allowed (full mesh within a group) and the
-- 'developer' group gets full agent-CRUD authority. Default '' (ungrouped)
-- keeps absence byte-equivalent to the pre-group-name behaviour.
CREATE TABLE IF NOT EXISTS node_comms_policy (
    name              TEXT PRIMARY KEY,
    outbound_siblings TEXT NOT NULL DEFAULT 'allow',
    outbound_parent   TEXT NOT NULL DEFAULT 'allow',
    inbound_siblings  TEXT NOT NULL DEFAULT 'allow',
    inbound_parent    TEXT NOT NULL DEFAULT 'allow',
    lineage_group     TEXT NOT NULL DEFAULT '',
    may_spawn         INTEGER NOT NULL DEFAULT 1,
    group_name        TEXT NOT NULL DEFAULT '',
    group_names       TEXT NOT NULL DEFAULT '',
    updated_at        REAL NOT NULL
);
"""
