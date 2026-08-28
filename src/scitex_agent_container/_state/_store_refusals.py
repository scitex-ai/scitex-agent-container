#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/_store_refusals.py
"""The fourth category: sac state that MUST NOT replicate, and why.

Split out of :mod:`._store_plugin` (which re-exports :data:`NEVER_SYNCED`,
so every existing import keeps resolving) under the per-file line cap, when
``sac_instances`` went live and its declaration grew the measured reasoning
that had until then been a plan.

The split is along the seam the parent module already draws. ``CLASSIFIED``
and ``SOURCE_TABLE`` say what sac's replicated rows MEAN; this file says
what is deliberately NOT replicated. A refusal is a design decision and
belongs in the declaration rather than in a reviewer's memory — and the two
halves are read at different moments: one when adding a store, the other
when someone asks why a table they can see is not on the sync path.

WHY ENTRIES OUTLIVE THEIR TABLES
================================
Several names here no longer exist as SQLite tables at all. They STAY.
The completeness gate only checks that every ``KNOWN_TABLES`` name is
decided, so a table LEAVING that tuple must not read as its refusal being
withdrawn: a store that moved backend still must not replicate, and deleting
the reason would lose why.
"""

from __future__ import annotations

__all__ = ["NEVER_SYNCED"]


#: Tables in sac's state DB that MUST NOT replicate, and why.
#:
#: A refusal is a design decision and belongs in the declaration, not in a
#: reviewer's memory. Two of these would be actively harmful to sync and
#: one is merely worthless; the reason field is what lets a future reader
#: tell those apart, exactly as it does for ``_system_deps``.
NEVER_SYNCED: dict[str, str] = {
    "node_tokens": (
        "bearer SECRETS: replicating one hands every host the credentials to "
        "impersonate every agent on every other host. Table REMOVED 2026-08-28, "
        "feature never armed (zero minters, 0 rows on every host); refusal KEPT "
        "-- a name leaving KNOWN_TABLES must not read as the refusal withdrawn"
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
        "the per-sample heartbeat STREAM, whose fleet-relevant content is "
        "one number: the latest. That number is carried as "
        "sac_instances.last_heartbeat_at under MergeRule.MAX, so syncing "
        "the stream would move the same fact at many times the cost. The "
        "original wording said 'thousands of rows per agent per day'; it "
        "was never thousands and never one — since 2026-08-28 it is not a "
        "SQLite table either. It left KNOWN_TABLES with its writer and "
        "reader, both of which had zero callers in src/, and it held 0 "
        "rows on every host measured. None of that withdraws the refusal: "
        "if a heartbeat stream is ever written again it must not sync"
    ),
    "attempts": (
        "a legacy actions.db carry-over with ZERO writers anywhere in src/ "
        "— replicating a table nothing writes moves no information. Since "
        "2026-08-28 it is not a SQLite table either: it left KNOWN_TABLES "
        "and its DDL was deleted, which does not change the ruling"
    ),
    "definitions": (
        "same: never INSERTed by any code path. This entry used to open "
        "'in KNOWN_TABLES, FK'd from instances.definition_id' — since "
        "2026-08-28 it is neither: the table was deleted on exactly the "
        "evidence recorded here, and instances.definition_id keeps its "
        "(all-NULL) column without the REFERENCES clause. The ruling is "
        "unchanged, which is why the entry stays: sync it only once "
        "something writes it; a spec is a promise and its truth is the "
        "YAML on disk"
    ),
    "events": (
        "per-host lifecycle log carrying only kind='start'/'stop', both of "
        "which are already the started_at/ended_at columns of the "
        "sac_instances row it points at. Its autoincrement id would also "
        "collide across hosts, so it costs a key rewrite to move a fact "
        "that is already replicated. Since 2026-08-28 it is not a SQLite "
        "table either — deleted for having zero readers, on the same "
        "already-replicated argument this refusal rests on — and the rows "
        "it left on existing databases must still not sync"
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
    # MOST OF THIS DICT NO LONGER APPEARS IN KNOWN_TABLES, and that is fine.
    # The diary trio left SQLite on 2026-08-28; ``attempts``, ``node_tokens``,
    # ``definitions``, ``instance_heartbeats`` and ``events`` were deleted
    # over the same few days. Every one of them STAYS here for the reason
    # acl_deny_notify_log stays: the completeness gate only checks that every
    # KNOWN_TABLES name is decided, so a table leaving that tuple must not be
    # read as the refusal being withdrawn. A store that moved backend — or a
    # table that was deleted outright — still must not replicate, and
    # deleting the reason would lose why. The tense of each entry is updated
    # instead, so a reader can tell "refused, and no longer exists" from
    # "refused, and live".
}

# EOF
