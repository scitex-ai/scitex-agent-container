#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_instances_rename.py
"""The agent-rename step for ``instances`` records, and its inverse.

Split out of :mod:`.state_db_instances` under the per-file line cap, the same
way :mod:`.state_db_instances_store` is.

WHY THIS IS A RENAME *STEP* AND NOT THREE MORE ``NAME_COLUMNS`` PAIRS
=====================================================================
``_lifecycle._rename_db.NAME_COLUMNS`` used to carry ``("instances",
"name")`` and ``("instances", "spawned_by")``, and ``PATH_COLUMNS`` carried
``("instances", "workdir")``. Leaving them there after the move would have
been WORSE than a crash, for exactly the reason the ``comms_nodes`` and
``node_comms_policy`` pairs were removed before it: ``rename_rows`` SKIPS a
table absent from ``sqlite_master``, so the rename would have reported
SUCCESS while every record kept the OLD name.

What that costs is not cosmetic. ``list_active_instances`` is the oracle
behind ``sac agents list``, the start preflight's "is it already running"
check, the stale-lease sweep, the reconciler and the restart verifier. An
agent renamed with those records left behind starts under the new name while
all six keep answering about the old one — and the preflight, seeing no live
record for the new name, will happily start a SECOND copy.

WHY ``spawned_by`` IS ``LAST_WRITER_WINS`` AND THE PLUGIN NOW SAYS SO
======================================================================
:mod:`.._store_plugin` declared ``spawned_by`` IMMUTABLE while
``sac_instances`` was still a plan. Measured against this step, that
declaration is not implementable:

* ``merge_field`` freezes an IMMUTABLE field at its first stamped value and
  reports a ``MergeConflict`` for any later, different one — WITHOUT
  raising. ``Store.put`` returns the conflict in ``PutResult.conflicts``,
  which no caller inspects.
* So renaming a parent agent would leave every child's ``spawned_by``
  pointing at the dead name, silently, while this step reported success —
  the precise silent no-op the step exists to prevent.

The alternative was to exclude ``spawned_by`` from the rename and say so.
Rejected: ``_lifecycle/_status`` reads it as the lineage edge and
``sac agents rename`` already covered it under SQLite, so excluding it would
retire live coverage in a storage migration.

Declaring it LAST_WRITER_WINS costs nothing that IMMUTABLE was buying,
because IMMUTABLE was never buying replication safety HERE: ``instances`` is
PER_HOST truth with ``host`` in the record identity and ``SINGLE_WRITER``
ownership, so two hosts cannot write one record at all. The merge rule on
this field can only ever decide a SAME-HOST rewrite — which is exactly this
rename. The family-tree DAG's authoritative record is ``sac_lineage``, where
``parent_name`` stays IMMUTABLE and a genuine contradiction still surfaces
as a ``MergeConflict``.

This is the same shape as :mod:`.state_db_incarnations`, which likewise
declares LAST_WRITER_WINS where the plugin says IMMUTABLE, and for the same
class of reason: a real write path the plan had not met yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .state_db_instances import scan_instances
from .state_db_instances_store import ACTOR, instance_key, run_with_reconnect

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "InstancesRenameUndo",
    "count_instance_rename_rows",
    "rename_instance_rows",
    "undo_rename_instance_rows",
]


@dataclass
class InstancesRenameUndo:
    """Key-scoped inverse of a completed :func:`rename_instance_rows`.

    Records the exact identities touched, per field, BEFORE touching them —
    the same discipline ``_lifecycle._rename_db`` applies with rowids, and
    for the same reason. "Run the rename backwards" is NOT a correct undo:
    it would also rewrite records that legitimately held the NEW name before
    the rename ran (the recorded lifetimes of a previously deleted agent by
    that name).
    """

    old: str
    new: str
    #: identity dicts whose ``name`` was rewritten
    names: list[dict] = field(default_factory=list)
    #: identity dicts whose ``spawned_by`` was rewritten
    spawned_by: list[dict] = field(default_factory=list)
    #: ``(identity, previous_workdir)`` for every rewritten path
    workdirs: list[tuple[dict, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.names) + len(self.spawned_by) + len(self.workdirs)


def _names_component(value: Any, old: str) -> bool:
    """True when ``value`` is a path with ``old`` as a WHOLE component.

    Mirrors ``_rename_db._has_component``. A component that merely CONTAINS
    the name is never touched, which is what keeps the rewrite from mangling
    ``…/scitex-todo-archive/…``.
    """
    return isinstance(value, str) and old in value.split("/")


def rename_instance_rows(*, old: str, new: str) -> InstancesRenameUndo:
    """Point every ``old`` record at ``new``. Returns its inverse.

    Three fields carry the agent name and all three move together:

    ``name``
        the agent this recorded lifetime belonged to.
    ``spawned_by``
        the lineage edge — the CHILDREN of a renamed parent name it.
    ``workdir``
        a path with the name as a whole component (``…/proj/<name>``),
        rewritten component-wise by
        :func:`.._lifecycle._rename_spec.sub_path`.

    ``id``/``host`` are the IDENTITY and are untouched: a renamed agent is
    the SAME agent and its recorded lifetimes are the same lifetimes. That is
    the difference from ``rename_comms_node``, where the name IS the identity
    and a rename is therefore one record ending and another beginning.

    NOTHING IS REFUSED HERE, deliberately. ``_rename_db`` turned a UNIQUE
    violation into ``DbRenameError`` because ``comms_nodes.name`` was a
    PRIMARY KEY. ``instances`` never had uniqueness on ``name`` — only a
    partial index on ``(name, host, scope)`` — so there was no constraint to
    trip and the store adds none. Records for a previously-deleted agent
    called ``new`` are history, not a live claim; the collision that WOULD
    matter, two live agents answering to one name, is refused one step
    earlier by ``rename_comms_node``.
    """
    undo = InstancesRenameUndo(old=old, new=new)
    if not old or not new or old == new:
        return undo

    from scitex_dev.store import ANY_REVISION

    from .._lifecycle._rename_spec import sub_path

    def _rename(store: "Store") -> InstancesRenameUndo:
        for row in scan_instances(store):
            values = row.values
            key = instance_key(values)
            payload: dict[str, Any] = {}
            if values.get("name") == old:
                payload["name"] = new
                undo.names.append(key)
            if values.get("spawned_by") == old:
                payload["spawned_by"] = new
                undo.spawned_by.append(key)
            workdir = values.get("workdir")
            if _names_component(workdir, old):
                rewritten = sub_path(str(workdir), old, new)
                if rewritten != workdir:
                    payload["workdir"] = rewritten
                    undo.workdirs.append((key, str(workdir)))
            if payload:
                store.put(
                    {**key, **payload}, expected_revision=ANY_REVISION, actor=ACTOR
                )
        return undo

    return run_with_reconnect(_rename)


def undo_rename_instance_rows(undo: InstancesRenameUndo) -> None:
    """Restore every record :func:`rename_instance_rows` touched, by key."""
    if undo.total == 0:
        return

    from scitex_dev.store import ANY_REVISION

    def _restore(store: "Store") -> None:
        for key in undo.names:
            store.put(
                {**key, "name": undo.old}, expected_revision=ANY_REVISION, actor=ACTOR
            )
        for key in undo.spawned_by:
            store.put(
                {**key, "spawned_by": undo.old},
                expected_revision=ANY_REVISION,
                actor=ACTOR,
            )
        for key, before in undo.workdirs:
            store.put(
                {**key, "workdir": before},
                expected_revision=ANY_REVISION,
                actor=ACTOR,
            )

    run_with_reconnect(_restore)


def count_instance_rename_rows(*, old: str) -> dict[str, int]:
    """``{"instances.<field>": n}`` for what a rename would touch. READ-ONLY.

    The store half of ``_rename_db.count_rows``, which is what ``sac agents
    rename --dry-run`` prints. Reported under the same ``table.column`` keys
    the SQLite counts use, so the operator reads one list rather than two —
    and so a zero here is a zero for the same reason it is everywhere else in
    that report, rather than "this half was not asked".
    """
    if not old:
        return {}
    counts = {"name": 0, "spawned_by": 0, "workdir": 0}

    def _count(store: "Store") -> None:
        for row in scan_instances(store):
            values = row.values
            if values.get("name") == old:
                counts["name"] += 1
            if values.get("spawned_by") == old:
                counts["spawned_by"] += 1
            if _names_component(values.get("workdir"), old):
                counts["workdir"] += 1

    run_with_reconnect(_count)
    return {f"instances.{k}": v for k, v in counts.items() if v}

# EOF
