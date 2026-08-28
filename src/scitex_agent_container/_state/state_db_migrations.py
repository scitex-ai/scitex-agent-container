"""Idempotent schema migrations for state.db.

Extracted from :mod:`state_db` so that module stays under the per-file
line cap. Each function takes an open :class:`sqlite3.Connection` and is
a no-op when its migration has already run, so they are safe to call on
every :func:`state_db.init_schema`.

  * :func:`migrate_instances_add_family_tree_cols` — ADD COLUMN the
    sac-agent-spawn family-tree columns (``bound_port``, ``remote``,
    ``spawned_by``) onto a pre-existing ``instances`` table.
"""

from __future__ import annotations

import sqlite3


# ``migrate_legacy_heartbeats`` and ``migrate_instance_heartbeats_add_seq``
# lived here until 2026-08-28. One renamed the original F-CS11 instance-tied
# ``heartbeats`` table onto ``instance_heartbeats``; the other rebuilt that
# table to add the monotonic ``seq`` PK that made "latest heartbeat"
# MAX(seq) rather than an arbitrary tie on a second-resolution ``ts``.
#
# ``instance_heartbeats`` left SQLite the same day — its writer
# ``update_heartbeat`` and its reader ``latest_instance_heartbeat`` had ZERO
# callers in ``src/``, and it held 0 rows on every host measured — so both
# migrations were left pointing at a table :mod:`.state_db_schema` no longer
# defines.
#
# THESE TWO ARE NOT THE ``node_comms_policy`` CASE BELOW, and the difference
# is why they had to be deleted rather than merely tidied. Those were
# permanent no-ops. These two could still FIRE: a state.db old enough to
# carry the legacy ``heartbeats`` name would have been renamed into
# ``instance_heartbeats``, and one without ``seq`` would have been rebuilt —
# both re-creating, on exactly the databases least able to explain where it
# came from, a table sac had just declared it does not maintain. A migration
# whose success restores something the schema deleted is not a safety net.


# ``migrate_instances_add_family_tree_cols`` lived here until 2026-08-28.
# It ALTERed ``instances`` to add ``bound_port`` / ``remote`` /
# ``spawned_by``, and that table moved to the shared PostgreSQL store in
# the same change — so it was already written to return early when the
# table is absent, and would have run as a permanent no-op for the rest of
# time. Deleted with the DDL rather than left to be read as a live schema
# step. (``bound_port`` did not even survive the move as a field: it and
# ``a2a_port`` always held one value, and the store keeps one.)


# ``migrate_node_comms_policy_add_group_name`` and
# ``migrate_node_comms_policy_add_group_names`` lived here until
# 2026-08-28. Both ALTERed ``node_comms_policy``, which moved to
# PostgreSQL in the same commit — so both were already written to
# return early when the table is absent, and would have run as
# permanent no-ops for the rest of time. A migration that can never
# fire is not a safety net; it is a claim that a schema step still
# happens. Deleted with the DDL rather than left to be read as live.
