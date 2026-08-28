#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_store_plugin.py
"""sac's LEAF declaration of what its state means when two hosts merge it.

scitex-dev owns the replication primitive (``scitex_dev.store``: oplog,
hybrid logical clock, per-origin cursors, the gapless
``first_seq == cursor + 1`` assertion, field-level merge). scitex-cards
declares the card semantics. This module is the third leaf: **sac says
what sac's rows MEAN**, and nothing else. It builds no sync mechanism,
opens no connection, and writes no SQL.

The whole design is one classification
======================================
Every row in sac's state DB is exactly one of three things, and the merge
rules are a consequence rather than a choice:

``PER_HOST``
    A host's own observation of its own processes. An agent's pid, its
    tmux/screen name, its heartbeat: compute-04 and spartan describing
    "the same agent" are describing **two different processes**, and each
    is right about itself. Two hosts disagreeing here is NOT a conflict to
    resolve — resolving it is the bug, because last-writer-wins would let
    one host's clock erase another host's true observation.

    Expressed by putting ``host`` in the record IDENTITY, so the two
    observations are different RECORDS that never meet in a merge at all,
    and by ``SINGLE_WRITER`` so only the observing host may write them.
    The primitive documents that exact case: *"Right for stores whose
    records have a genuine, stable owner — a host writing its own
    telemetry."*

``FLEET``
    One fact the whole fleet shares. "Agent X is reachable at host:port"
    is the same claim everywhere, so two hosts disagreeing IS a conflict
    and needs a stated rule.

``HISTORY``
    Append-only. Merging must never lose a branch. Each edge is its own
    record keyed by the child, so distinct edges never collide and none
    can be dropped; a genuine contradiction (two parents claimed for one
    child) is surfaced as a ``MergeConflict``, never silently resolved.

A fourth category is not synced at all, and saying so out loud is the
point — see :data:`NEVER_SYNCED`.

Why classification is the deliverable
-------------------------------------
sac already ships a cross-host path, and it is unsafe in a way that is
invisible: ``state_db_export.import_state`` does
``INSERT OR IGNORE INTO <t>``. A byte-identical duplicate and a row that
CONTRADICTS the local one both yield ``rowcount == 0``. The importer's
success value is also its didn't-happen value, so two hosts can disagree
permanently while every call returns 0 and reports success. That path is
what the primitive replaces; this module is the declaration that makes
the replacement safe rather than merely different.

Measured, not inferred (scitex-compute-04, 2026-08-12)::

    in-container  /state/scitex-agent-container/state.db
                  instances 0    events 0    channel_events 0     lineage 0
    bare host     ~/.scitex/agent-container/runtime/state.db
                  instances 192  events 366  channel_events 1872  lineage 4

Same host, same package, same schema, both readable, every call exit 0.
An empty read cannot distinguish "no rows anywhere" from "I only looked
in one place" — which is how, on 2026-08-09, three agents read the empty
shard and escalated it as P1 fleet-registry data loss.

Does sac's state know where it came from?
------------------------------------------
Asked because scitex-cards found its own ``origin_node`` declared at
SCHEMA_VERSION 11 and written by nothing — a column that answers "which
host is this row from?" and is empty on every row. A schema tells you a
field is PRESENT, never that it is FILLED, so the honest check is to grep
for WRITERS. sac's answer is mixed and worth stating plainly:

* ``instances`` — YES. ``host`` is written by ``record_instance_start``
  from the resolved canonical host, and it is populated by DEFAULT rather
  than only when a caller remembers to pass it. A test in this module's
  suite exercises the real writer and asserts the column lands non-empty,
  because the per-host identity below is worthless if it does not.
* ``turns`` / ``errors`` / ``heartbeats`` — YES, each carries a written
  ``host``. All three are refused for other reasons (see NEVER_SYNCED).
* ``lineage``, ``comms_grants``, ``node_comms_policy``, ``node_tokens``,
  ``channel_events`` — NO origin column of any kind exists.
* ``comms_nodes.source_host`` — was NULL for every locally registered row
  and set only on the PULL path; deleted in the 2026-08-28 move.

That gap is closed by adopting the primitive rather than by adding
columns here: ``_origin`` is a RESERVED column the store stamps on every
op from ``Store.node``, so provenance stops being something each leaf
has to remember to write.

But ``_origin`` is NOT a substitute for ``host``, and conflating them
would rebuild the bug in a new place. ``_origin`` is PROVENANCE — which
node told me this. ``host`` is SUBJECT — which machine the row is ABOUT.
They coincide for ``sac_instances`` only because SINGLE_WRITER makes the
observing host the only writer; the moment a row is relayed, replayed
from an oplog, or handed over on relocation, the two diverge. So ``host``
stays an explicitly declared IDENTITY field.
"""

from __future__ import annotations

from enum import Enum

from scitex_dev.store import (
    FieldKind,
    FieldPolicy,
    FieldRole,
    MergeRule,
    Schema,
    WriterPolicy,
)

_PROVIDER = "scitex-agent-container"
_PKG = "scitex-agent-container"


class Truth(str, Enum):
    """Whose fact a row is — the classification the merge rules follow from."""

    PER_HOST = "per_host"
    FLEET = "fleet"
    HISTORY = "history"


def _identity(kind: FieldKind) -> FieldPolicy:
    """An IDENTITY field. The primitive requires these be IMMUTABLE+required."""
    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=True,
    )


def _data(
    kind: FieldKind,
    merge: MergeRule,
    *,
    required: bool = False,
    indexed: bool = False,
) -> FieldPolicy:
    """A DATA field carrying an explicit merge rule."""
    return FieldPolicy(
        kind=kind, role=FieldRole.DATA, required=required, merge=merge, indexed=indexed
    )


# ---------------------------------------------------------------------------
# FLEET — one fact the fleet shares; disagreement is a real conflict.
# ---------------------------------------------------------------------------
# The cross-host directory (ADR-0014): "agent <name> is reachable at
# host:a2a_port". LIVE ON POSTGRESQL SINCE 2026-08-28, opened field for
# field by ``_state.state_db_comms_nodes_store``. It was the one table sac
# synced, and that sync was provably lossy: INSERT OR IGNORE carries
# neither an update nor a tombstone, so the old module admitted deletion
# propagation "will need an UPDATE-shaped sync (future work)". There is
# none — every host reads and writes THIS directory now, so the whole
# anti-entropy layer has nothing left to converge.
#
# Three hand-rolled columns are DELETED rather than declared, because the
# primitive owns each concept and a second copy drifts: ``ended_at`` (the
# soft tombstone — hide()/unhide() is "the ONLY removal", replicated as an
# op), ``source_host`` (provenance, NULL on every local row — ``_origin``
# is exactly this) and ``updated_at`` (the HLC restamps every op and is
# comparable across hosts, unlike a skewed wall clock).
#
# ``registered_at`` is IMMUTABLE deliberately: two hosts claiming one agent
# NAME with different registration times is precisely the collision sac
# raises CommsNodeConflictError for; IMMUTABLE reports it as a MergeConflict
# (kept/rejected/reason) rather than quietly picking one.
COMMS_NODES = Schema.build(
    "sac_comms_nodes",
    {
        "name": _identity(FieldKind.TEXT),
        "host": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True, indexed=True),
        "a2a_port": _data(FieldKind.INTEGER, MergeRule.LAST_WRITER_WINS, required=True),
        "registered_at": _data(FieldKind.REAL, MergeRule.IMMUTABLE, required=True),
    },
)

# ---------------------------------------------------------------------------
# HISTORY — append-only; merging must never lose a branch.
# ---------------------------------------------------------------------------
# One record per CHILD (a child has exactly one parent, ever), so distinct
# edges are distinct records and a union can drop none of them. The
# load-bearing choice is IMMUTABLE on ``parent_name``: if two hosts claim
# different parents for one child, that is a real contradiction about the
# spawn DAG and it surfaces as a MergeConflict carrying both values.
# LAST_WRITER_WINS here would silently rewrite the family tree, and the
# ACL derives group membership from it (check_lineage_acl), so a silent
# rewrite is a silent privilege change.
#
# MULTI_WRITER, not SINGLE_WRITER: a cross-host spawn is brokered, so the
# edge can legitimately be written by either end, and a second writer must
# get the loud MergeConflict rather than an ownership rejection that says
# nothing about WHY the two disagree.
LINEAGE = Schema.build(
    "sac_lineage",
    {
        "child_name": _identity(FieldKind.TEXT),
        "parent_name": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True, indexed=True),
        "created_at": _data(FieldKind.REAL, MergeRule.IMMUTABLE, required=True),
    },
)

# ---------------------------------------------------------------------------
# PER_HOST — each host is right about itself.
# ---------------------------------------------------------------------------
# ``host`` is an IDENTITY field, and that is the entire per-host-truth
# mechanism: it makes compute-04's row and spartan's row DIFFERENT
# RECORDS, so a merge between them is not resolved-in-favour-of-one, it
# never occurs. ``id`` (uuid7, minted at the origin) is also identity, so
# even repeated observations of one agent name stay separate lifetimes.
#
# The merge rules on the mutable fields are chosen so a STALE replica can
# never move a live host's truth backwards:
#   * last_heartbeat_at → MAX. An ISO-8601 UTC string sorts lexicographically,
#     so MAX is a high-water mark: an old sample arriving late cannot
#     resurrect a dead agent or rewind a live one. LAST_WRITER_WINS would
#     let a delayed delivery do exactly that.
#   * iter_count / input_tokens / output_tokens → MAX. Monotone counters.
#   * ended_at / exit_reason → IMMUTABLE. A process ends ONCE. A second,
#     different end time is not a later opinion, it is a contradiction —
#     and sac has already been burned by believing one: on 2026-08-11
#     eleven rows shared ended_at=2026-08-11T17:54:26Z and three readers
#     independently read that as a simultaneous mass kill. It was one
#     now_iso() evaluated once per GC sweep and stamped on every reaped
#     row; the agents had died 10h46m earlier. IMMUTABLE makes the second
#     stamp a reported conflict instead of a believed fact.
#   * started_at → IMMUTABLE for the same reason, in the other direction.
#
# SINGLE_WRITER: only the observing host may write its own telemetry. An
# agent RELOCATION (sac moves a running agent between hosts) is then not
# an illegal cross-host write but an explicit ownership handover() — which
# is the honest shape, because relocation already carries a fencing lease.
INSTANCES = Schema.build(
    "sac_instances",
    {
        "id": _identity(FieldKind.TEXT),
        "host": _identity(FieldKind.TEXT),
        "name": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True, indexed=True),
        "pid": _data(FieldKind.INTEGER, MergeRule.LAST_WRITER_WINS),
        "a2a_port": _data(FieldKind.INTEGER, MergeRule.LAST_WRITER_WINS),
        "spawned_by": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
        "started_at": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True),
        "last_heartbeat_at": _data(FieldKind.TEXT, MergeRule.MAX),
        "iter_count": _data(FieldKind.INTEGER, MergeRule.MAX),
        "input_tokens": _data(FieldKind.INTEGER, MergeRule.MAX),
        "output_tokens": _data(FieldKind.INTEGER, MergeRule.MAX),
        "ended_at": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
        "exit_reason": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
    },
)


# The birth certificate (v4 step 5, operator directive 2026-08-14): the
# COMPILED spec recorded at launch, keyed by incarnation id, plus the
# death mirror. PER_HOST truth with the SAME idiom as ``sac_instances``
# — the observing host is describing ITS OWN launch of ITS OWN process,
# so ``host`` joins the identity and SINGLE_WRITER keeps the birth host
# the only author — and it SYNCS, deliberately: an incarnation born on
# host A must be visible from host B (the record answers "how was this
# agent born", which the fleet asks about agents it did not launch), and
# the rows are write-once by construction (``incarnation_id`` is a
# globally-unique uuid7; every field is filled at birth or exactly once
# at death), so replication can never diverge — a second, DIFFERENT
# value for any field is a real contradiction and IMMUTABLE reports it
# as a MergeConflict instead of quietly picking one. The exit trio uses
# the same fill-once IMMUTABLE shape as ``instances.ended_at``: a
# process is born once and dies once.
INCARNATIONS = Schema.build(
    "sac_incarnations",
    {
        "incarnation_id": _identity(FieldKind.TEXT),
        "host": _identity(FieldKind.TEXT),
        "agent_id": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True, indexed=True),
        "spec_id": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
        "spec_git_sha": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True),
        "born_at": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True),
        "compiled_spec_json": _data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True),
        "exit_reason": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
        "exit_code": _data(FieldKind.INTEGER, MergeRule.IMMUTABLE),
        "exited_at": _data(FieldKind.TEXT, MergeRule.IMMUTABLE),
    },
)


# The two ACL tables. Both are FLEET truth, and both are declared rather
# than deferred because getting an ACL wrong is a PRIVILEGE bug: an ACL
# that silently diverges between hosts means the same agent is authorised
# on one host and refused on another, which reads as a flaky feature
# rather than as a security fault.
#
# comms_grants is the case that most needs the primitive. It is a SET, and
# its revocation is a raw DELETE today — a deletion INSERT OR IGNORE can
# never carry, so a revoked grant is resurrected by the next pull from any
# peer that still has it. Under the primitive, revoke is hide(), which
# replicates as an op like any other; that single change is the difference
# between a revocation that sticks and one that silently comes back.
COMMS_GRANTS = Schema.build(
    "sac_comms_grants",
    {
        "sender_name": _identity(FieldKind.TEXT),
        "target_name": _identity(FieldKind.TEXT),
        "created_at": _data(FieldKind.REAL, MergeRule.IMMUTABLE, required=True),
        "note": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS),
    },
)

# Per-agent capsule-isolation policy, upserted at every agent_start from
# the loaded spec. Its content is DERIVED from the spec, so the newest
# write is genuinely the best answer and LAST_WRITER_WINS is honest here —
# unlike on a heartbeat, where the newest DELIVERY is not the newest FACT.
# ``updated_at`` is LAST_WRITER_WINS too, matching the live store. It was
# MAX here, to stop a late-arriving stale replica walking the row's clock
# backwards -- but HLC ordering already delivers that for every other
# field, and MAX bought it by resolving this one field on a DIFFERENT
# ordering than the rest of the record. ``_merge.py`` decides
# LAST_WRITER_WINS on ``incoming_stamp > current_stamp`` (the HLC) and MAX
# on ``incoming > current`` (the value), and those disagree whenever a
# node's HLC wall has been pulled forward by a peer's message while its own
# ``time.time()`` -- which is what ``updated_at`` holds -- has not. MAX then
# keeps the LOSING write's larger timestamp and the record advertises itself
# as fresher than the write that supplied its data: a stale ACL reading as
# current. See ``state_db_acl_policy_store`` for the same argument at the
# point the value is actually written.
NODE_COMMS_POLICY = Schema.build(
    "sac_node_comms_policy",
    {
        "name": _identity(FieldKind.TEXT),
        "outbound_siblings": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "outbound_parent": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "inbound_siblings": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "inbound_parent": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "lineage_group": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "may_spawn": _data(FieldKind.BOOL, MergeRule.LAST_WRITER_WINS, required=True),
        # BOTH group projections. They are written together by
        # ``record_comms_policy`` from one ``metadata.labels``, and
        # declaring only ``group_names`` here let the primary drift out
        # of the replication contract while the mesh still resolved
        # through it.
        "group_name": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "group_names": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
        "updated_at": _data(FieldKind.REAL, MergeRule.LAST_WRITER_WINS, required=True),
    },
)


#: Every schema sac declares, with the classification it follows from.
CLASSIFIED: dict[str, tuple[Schema, Truth, WriterPolicy]] = {
    # LIVE since 2026-08-28 — the move made this classification real rather
    # than planned. SINGLE_WRITER survived it: the ownership check is on the
    # ACTOR (a package constant), so cross-host operator repair still works.
    "sac_comms_nodes": (COMMS_NODES, Truth.FLEET, WriterPolicy.SINGLE_WRITER),
    "sac_comms_grants": (COMMS_GRANTS, Truth.FLEET, WriterPolicy.MULTI_WRITER),
    # MULTI_WRITER since 2026-08-28, when the table moved to PostgreSQL
    # and this declaration stopped being a plan and started describing a
    # live store. SINGLE_WRITER modelled the record as owned by the host
    # running the agent, which it is not: ``sac agents refresh-acl``
    # re-publishes the whole fleet from wherever the operator is, agents
    # relocate between hosts, and ``import_state`` bulk-imports peer rows.
    # A refused ACL write is a STALE ACL row, and a stale ACL row denies
    # an agent its groups or leaves it holding groups its spec dropped.
    "sac_node_comms_policy": (
        NODE_COMMS_POLICY,
        Truth.FLEET,
        WriterPolicy.MULTI_WRITER,
    ),
    "sac_lineage": (LINEAGE, Truth.HISTORY, WriterPolicy.MULTI_WRITER),
    "sac_instances": (INSTANCES, Truth.PER_HOST, WriterPolicy.SINGLE_WRITER),
    "sac_incarnations": (INCARNATIONS, Truth.PER_HOST, WriterPolicy.SINGLE_WRITER),
}


#: Which sac state table each declared schema replicates, so the
#: classification can be checked for COMPLETENESS against the real DB
#: rather than against this module's own opinion of it.
SOURCE_TABLE: dict[str, str] = {
    "sac_comms_nodes": "comms_nodes",
    "sac_comms_grants": "comms_grants",
    "sac_node_comms_policy": "node_comms_policy",
    "sac_lineage": "lineage",
    "sac_instances": "instances",
    "sac_incarnations": "incarnations",
}


#: Tables in sac's state DB that MUST NOT replicate, and why.
#:
#: A refusal is a design decision and belongs in the declaration, not in a
#: reviewer's memory. Two of these would be actively harmful to sync and
#: one is merely worthless; the reason field is what lets a future reader
#: tell those apart, exactly as it does for ``_system_deps``.
NEVER_SYNCED: dict[str, str] = {
    "node_tokens": (
        "bearer SECRETS. A token is the authenticated identity the listen "
        "server resolves Authorization: Bearer against; replicating it "
        "hands every host the credentials to impersonate every agent on "
        "every other host, turning one host's compromise into the fleet's"
    ),
    "channel_events": (
        "the autoincrement id IS the SSE cursor a client passes back as "
        "Last-Event-ID. Interleaving another host's numbering silently "
        "changes what 'resume from N' means, so a reconnecting client "
        "skips or replays frames with no error anywhere"
    ),
    "acl_deny_notify_log": (
        "a per-host rate-limit ledger (last_notified_at) — since 2026-08-20 a "
        "per-host PostgreSQL store rather than a SQLite table, which does not "
        "change the ruling. Merging it suppresses a deny-notification on a "
        "host that never sent one — the failure is a notification that does "
        "NOT arrive, which is invisible by construction"
    ),
    "instance_heartbeats": (
        "the per-sample heartbeat STREAM, thousands of rows per agent per "
        "day, whose fleet-relevant content is one number: the latest. That "
        "number is carried as sac_instances.last_heartbeat_at under "
        "MergeRule.MAX, so syncing the stream would move the same fact at "
        "thousands of times the cost"
    ),
    "attempts": (
        "a legacy actions.db carry-over with ZERO writers anywhere in src/ "
        "— replicating a table nothing writes moves no information. Since "
        "2026-08-28 it is not a SQLite table either: it left KNOWN_TABLES "
        "and its DDL was deleted, which does not change the ruling"
    ),
    "definitions": (
        "same: in KNOWN_TABLES, FK'd from instances.definition_id, and "
        "never INSERTed by any code path. Sync it only once something "
        "writes it; a spec is a promise and its truth is the YAML on disk"
    ),
    "events": (
        "per-host lifecycle log carrying only kind='start'/'stop', both of "
        "which are already the started_at/ended_at columns of the "
        "sac_instances row it points at. Its autoincrement id would also "
        "collide across hosts, so it costs a key rewrite to move a fact "
        "that is already replicated"
    ),
    "turns": (
        "the agent conversation diary — prompt_text and response_text, i.e. "
        "the full content of what agents were asked and answered. Since "
        "2026-08-28 a per-host PostgreSQL store rather than a SQLite table, "
        "which does not change the ruling: high-volume per-host diagnostics "
        "whose content is the most sensitive thing sac records, and it "
        "should not leave its host as a side effect of a directory sync"
    ),
    "errors": (
        "per-host error journal, since 2026-08-28 a per-host PostgreSQL "
        "store rather than a SQLite table. Useful to READ across hosts, but "
        "that is a query concern; replicating it puts an unbounded "
        "diagnostic stream on the sync path"
    ),
    "heartbeats": (
        "the diary-style heartbeat stream (name, host, pid, state, ts), "
        "append-only, and since 2026-08-28 a per-host PostgreSQL store "
        "rather than a SQLite table. Same argument as instance_heartbeats: "
        "the fleet-relevant content is the latest sample, carried as "
        "sac_instances.last_heartbeat_at"
    ),
    # The three entries above — and ``attempts`` — no longer appear in
    # KNOWN_TABLES: the diary left SQLite on 2026-08-28 and ``attempts``
    # left the same day. They STAY here for the reason
    # acl_deny_notify_log stays: the completeness gate only checks that every
    # KNOWN_TABLES name is decided, so a table leaving that tuple must not be
    # read as the refusal being withdrawn. A store that moved backend still
    # must not replicate, and deleting the reason would lose why.
}


def provide() -> list:
    """sac's store plugins, for ``scitex_dev.store.discover_store_plugins``.

    Returns ``list[StorePlugin]``. The import is deliberately local and
    deliberately NOT guarded: ``StorePlugin`` ships with the store
    federation, and a leaf that silently degraded to "no plugins" when the
    federation is absent would be indistinguishable from a leaf that
    declares nothing — the exact success-shaped failure this whole module
    exists to avoid. The discoverer already skips a raising provider with
    a logged warning, so the loud path is also the safe one.
    """
    from scitex_dev.store import StorePlugin

    return [
        StorePlugin(
            name=name,
            pkg=_PKG,
            schema=schema,
            writer_policy=policy,
            provider=_PROVIDER,
            description=f"sac {truth.value} state",
        )
        for name, (schema, truth, policy) in CLASSIFIED.items()
    ]


__all__ = [
    "CLASSIFIED",
    "COMMS_NODES",
    "INCARNATIONS",
    "INSTANCES",
    "LINEAGE",
    "NEVER_SYNCED",
    "Truth",
    "provide",
]

# EOF
