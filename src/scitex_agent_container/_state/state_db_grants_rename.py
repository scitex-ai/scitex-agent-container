#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_grants_rename.py
"""The agent-rename step for ``comms_grants`` records, and its inverse.

WHY THIS EXISTS AT ALL — THE PAIR THE MIGRATION MISSED
=======================================================
``comms_nodes``, ``node_comms_policy`` and ``instances`` each had their
``(table, column)`` pairs REMOVED from ``_lifecycle._rename_db.NAME_COLUMNS``
when they moved to the store, and each grew a rename step like this one.
``comms_grants`` moved on 2026-08-28 and its two pairs were LEFT BEHIND::

    ("comms_grants", "sender_name")
    ("comms_grants", "target_name")

``rename_rows`` SKIPS a table absent from ``sqlite_master``, so the rename
reported success while every grant kept the OLD name. Measured 2026-08-30 on a
state.db built exactly as the package builds one::

    NAME_COLUMNS targets:         ['comms_grants']
    _existing_tables sees:        (none)
    count_rows('old-agent') ->    {}

``{}`` is the whole defect in one line: ``--dry-run`` reports nothing to
rename, and the real rename then rewrites nothing.

WHAT IT COSTS. The grants are live — ``list_comms_grants()`` returns real rows
today. They are the explicit cross-group authorisations behind
``_listen._acl``: a renamed agent starts answering to the new name while every
grant still names the old one, so its permitted sends begin to be DENIED. The
failure surfaces later, as a permission error nobody connects to the rename.

WHY THIS IS ``rename_comms_node``'s SHAPE, NOT ``rename_instance_rows``'s
=========================================================================
The directed pair IS the record identity here (both fields are ``_ident``), so
a rename is not a field update: it is one record ending and another beginning.
``instances`` renames a NON-identity ``name`` column and can therefore ``put``
in place; this module cannot.

``created_at`` is carried forward rather than restamped, for the reason
``rename_comms_node`` carries ``registered_at``: a renamed agent is the SAME
agent, and when the grant was made is a fact about the authorisation, not about
the rename.

WHY A LIVE OCCUPANT IS **NOT** REFUSED HERE, unlike ``comms_nodes``
====================================================================
``rename_comms_node`` raises when ``new`` is already registered, because two
agents sharing one directory entry makes the victim unreachable. A grant is not
exclusive: ``(new → target)`` already existing LIVE means that authorisation is
already in force, so folding ``old``'s grant into it changes nothing a caller
can observe. Refusing would block a legitimate rename over a no-op. The old
record is still withdrawn, because a live grant naming an agent that no longer
exists is an authorisation nobody owns.

A REVOKED occupant is un-hidden rather than left alone: the operator revoked
``new → target`` at a time when ``new`` was a different agent (or did not
exist), and carrying ``old``'s LIVE grant across must not be silently downgraded
into a revoked one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .state_db_grants import _ACTOR, _open

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "GrantsRenameUndo",
    "count_grant_rename_rows",
    "rename_grant_rows",
    "undo_rename_grant_rows",
]


@dataclass
class GrantsRenameUndo:
    """Key-scoped inverse of a completed :func:`rename_grant_rows`.

    Records the exact identities touched BEFORE touching them, the discipline
    ``_rename_db`` applies with rowids and ``state_db_instances_rename`` with
    keys. "Run the rename backwards" is NOT a correct undo: it would also
    withdraw grants that legitimately named ``new`` before the rename ran.
    """

    old: str
    new: str
    #: identities CREATED under the new name, to be hidden on undo
    created: list[dict] = field(default_factory=list)
    #: identities HIDDEN under the old name, to be un-hidden on undo
    hidden: list[dict] = field(default_factory=list)
    #: identities that already existed under the new name and were UN-HIDDEN
    #: by this rename — re-hidden on undo, since they were revoked before it
    unhidden: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.hidden) + len(self.unhidden)


def _renamed(value: Any, old: str, new: str) -> str:
    """``new`` when ``value`` is exactly ``old``, else ``value`` unchanged.

    Whole-value equality, never a substring: an agent called ``lead`` must not
    rewrite a grant naming ``lead-archive``.
    """
    return new if value == old else str(value)


def _pairs_naming(store: "Store", old: str) -> list[tuple[dict, dict]]:
    """``(key, values)`` for every LIVE grant naming ``old`` on either side.

    Hidden rows are excluded: a revoked grant is history, and history keeps
    the name it was made under.
    """
    out: list[tuple[dict, dict]] = []
    for row in store.rows():
        values = row.values
        sender, target = str(values["sender_name"]), str(values["target_name"])
        if old in (sender, target):
            out.append(({"sender_name": sender, "target_name": target}, dict(values)))
    return out


def rename_grant_rows(*, old: str, new: str) -> GrantsRenameUndo:
    """Carry every ``old`` grant onto ``new``. Returns its inverse.

    The agent may appear as SENDER, as TARGET, or as both (a self-grant), and
    all three cases move together.
    """
    undo = GrantsRenameUndo(old=old, new=new)
    if not old or not new or old == new:
        return undo

    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    store = _open()
    try:
        for key, values in _pairs_naming(store, old):
            moved = {
                "sender_name": _renamed(values["sender_name"], old, new),
                "target_name": _renamed(values["target_name"], old, new),
            }
            if moved == key:  # pragma: no cover - _pairs_naming guarantees a hit
                continue

            # include_hidden: a revoked row still occupies the identity, so a
            # plain read says "absent" and the insert below would collide.
            occupant = store.get(moved, include_hidden=True)
            if occupant is None:
                store.put(
                    {
                        **moved,
                        "created_at": float(values["created_at"]),
                        "note": values.get("note"),
                    },
                    expected_revision=NEW_RECORD,
                    actor=_ACTOR,
                )
                undo.created.append(moved)
            elif store.is_hidden(moved):
                store.unhide(moved, expected_revision=ANY_REVISION, actor=_ACTOR)
                undo.unhidden.append(moved)
            # else: already live — the authorisation is in force, nothing to do.

            store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
            undo.hidden.append(key)
        return undo
    finally:
        store.close()


def undo_rename_grant_rows(undo: GrantsRenameUndo) -> None:
    """Restore every record :func:`rename_grant_rows` touched, by key."""
    if undo.total == 0:
        return

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        for key in undo.hidden:
            store.unhide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        for key in undo.unhidden:
            store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        for key in undo.created:
            store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
    finally:
        store.close()


def count_grant_rename_rows(*, old: str) -> dict[str, int]:
    """``{"comms_grants.<field>": n}`` for what a rename would touch. READ-ONLY.

    The store half of ``_rename_db.count_rows``, which is what
    ``sac agents rename --dry-run`` prints. Reported under the same
    ``table.column`` keys the SQLite counts used, so the operator reads one
    list rather than two.
    """
    if not old:
        return {}
    counts = {"sender_name": 0, "target_name": 0}

    store = _open()
    try:
        for _key, values in _pairs_naming(store, old):
            if values["sender_name"] == old:
                counts["sender_name"] += 1
            if values["target_name"] == old:
                counts["target_name"] += 1
    finally:
        store.close()
    return {f"comms_grants.{k}": v for k, v in counts.items() if v}


# EOF
