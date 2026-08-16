#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migrate an agent's task cards when it is renamed — via scitex-todo's port.

THE ORPHANING BUG THIS EXISTS TO PREVENT
----------------------------------------
An agent's identity on the board is ``SCITEX_TODO_AGENT_ID``. Change it
(as any rename must) without moving the cards, and every card the agent
owns still says ``agent: <old>`` / ``scope: agent:<old>``. The agent under
its new name can no longer see its own work — and NOTHING tells you.
Measured on the live board before this verb existed: the ``scitex-todo``
agent owned 158 cards, 84 of them scoped ``agent:scitex-todo``. A hand
rename orphans all 158, silently.

PORTS AND ADAPTERS — WHY THERE IS NO REASSIGN LOGIC IN THIS FILE
----------------------------------------------------------------
The board belongs to scitex-todo. It already owns the primitive:
``scitex_todo._store.reassign_task(store, task_id, new_owner, by=...)``,
which in ONE locked write sets ``agent = assignee = new_owner`` AND
``scope = "agent:<new_owner>"``, appends an audit comment, and emits the
canonical ``reassigned`` card-event. sac CALLS that. It does not reproduce
it. A forked copy of another package's store logic drifts into a worse
version of it.

The lazy import mirrors the existing precedent
(``_account/refresh_alarm`` imports ``scitex_todo._help_wait.help_wait``)
and is covered by the cross-package import gate,
``tests/integration/test_cross_package_imports.py``.

THE MISSING PRIMITIVE (and why the loop below is a placeholder, not a fork)
--------------------------------------------------------------------------
scitex-todo exposes no BULK verb: there is no ``reassign_all(old, new)``.
So this adapter loops ``reassign_task`` once per card. Profiling ONE warm
card write against a ONE-card store::

    3.24s total
      2.18s  _emit_card_event -> dispatch_event -> _iter_entry_points
               -> importlib.metadata.entry_points()   # ~126 files, UNCACHED
      0.89s  _save_doc_unlocked -> _git_autocommit_store  # 2 subprocess forks

That is FIXED overhead — it does not scale with the store, it is paid in
full on every single call. Migrating the 158 cards that motivated this
verb therefore costs ~158 x 3.2 s ~= 8.5 MINUTES, for what is semantically
ONE operation. A bulk verb on scitex-todo's side would be one lock, one
store write, one git commit, one coherent event: seconds.

That cost is the signature of a primitive living on the WRONG SIDE of the
port. ``reassign_all`` belongs in scitex-todo, NOT here. sac must not "fix"
it locally by reaching into the store's YAML — that IS the fork, and a
forked store writer drifts into a worse one. When scitex-todo ships the
bulk verb, :func:`migrate_cards` collapses to a single call.

(The uncached ``entry_points()`` rescan is a separate, standalone
scitex-todo bug — a one-line ``functools.cache`` would make EVERY card
write in the fleet ~2.2 s faster, not just this verb's.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CardMigrationError(RuntimeError):
    """The board could not be read or migrated."""


@dataclass
class CardMigration:
    """What :func:`migrate_cards` actually moved — the undo record."""

    old: str
    new: str
    store: str | Path | None
    moved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.moved)


def _store_module() -> Any:
    """Import scitex-todo's store, or fail LOUD.

    Silently degrading to "no cards migrated" is the exact bug this verb
    exists to prevent, so a missing board package is an error the operator
    must acknowledge (``--no-cards``), never a shrug.
    """
    try:
        from scitex_cards import _store
    except ImportError as exc:
        raise CardMigrationError(
            "scitex-todo is not installed, so this rename cannot see the "
            "board — and a rename that changes SCITEX_TODO_AGENT_ID without "
            "migrating cards ORPHANS every card the agent owns. Install "
            "scitex-todo, or re-run with --no-cards to accept that risk "
            "explicitly."
        ) from exc
    return _store


def _query(store: str | Path | None, **filters) -> list[dict]:
    """One ``list_tasks`` call, with the store failure surfaced loudly."""
    todo = _store_module()
    try:
        return todo.list_tasks(store, **filters)
    except Exception as exc:  # noqa: BLE001 - surface any store failure
        raise CardMigrationError(f"could not read the board ({filters}): {exc}") from exc


def find_owned_cards(old: str, *, store: str | Path | None = None) -> list[str]:
    """Return the ids of every card ``old`` OWNS — the ones a rename orphans.

    Ownership is the ``agent`` field, or the legacy ``assignee`` for a card
    written before the two were kept in lock-step. Both are queried and
    unioned. ``scope`` is DERIVED from the owner (``reassign_task`` sets
    ``scope = "agent:<owner>"``), so it is not an ownership signal and is
    deliberately not queried here — see :func:`find_foreign_scoped_cards`
    for why treating it as one would let a rename STEAL another agent's card.

    ``scope=""`` is LOAD-BEARING, not noise. ``list_tasks(scope=None)`` does
    not mean "no scope filter" — it means "fall back to
    ``$SCITEX_TODO_SCOPE``", which a sac-launched agent may well have set.
    Leaving it None would silently AND every owner query with the CALLER's
    own scope, so a rename run from inside one agent's container would find
    only the cards that happen to share its slice and orphan the rest —
    precisely the bug this module exists to prevent.

    Read-only. This is what ``--dry-run`` counts.
    """
    ids: dict[str, None] = {}  # ordered set
    for filters in ({"agent": old}, {"assignee": old}):
        for row in _query(store, scope="", **filters):
            task_id = row.get("id")
            if task_id:
                ids[str(task_id)] = None
    return list(ids)


def find_foreign_scoped_cards(
    old: str, *, store: str | Path | None = None
) -> list[str]:
    """Cards scoped ``agent:<old>`` that ``old`` does NOT own.

    Inconsistent data — ``scope`` is supposed to track the owner — but it
    exists in the wild, and it is a trap. The naive reading of "migrate
    everything scoped to the old agent" would hand these cards to the NEW
    name, taking them from whoever actually owns them. sac will not steal a
    card. It reports them instead, so the operator can see that the scope
    string ``agent:<old>`` will be left dangling on someone else's work.

    Read-only; surfaced as a plan warning.
    """
    owned = set(find_owned_cards(old, store=store))
    scoped = _query(store, scope=f"agent:{old}")
    return [
        str(row["id"])
        for row in scoped
        if row.get("id") and str(row["id"]) not in owned
    ]


def migrate_cards(
    old: str,
    new: str,
    *,
    store: str | Path | None = None,
    by: str | None = None,
) -> CardMigration:
    """Reassign every card owned by ``old`` to ``new``.

    Delegates each card to ``scitex_todo._store.reassign_task`` — see the
    module docstring for why sac does not do this itself.

    Raises:
        CardMigrationError: Any card failed to move. The migration record
            of the cards that DID move is attached as ``.migration`` so the
            caller can roll them back.
    """
    todo = _store_module()
    migration = CardMigration(old=old, new=new, store=store)
    for task_id in find_owned_cards(old, store=store):
        try:
            result = todo.reassign_task(
                store, task_id=task_id, new_owner=new, by=by
            )
        except Exception as exc:  # noqa: BLE001 - any store failure aborts
            err = CardMigrationError(
                f"card {task_id!r} could not be reassigned to {new!r}: {exc}"
            )
            err.migration = migration  # type: ignore[attr-defined]
            raise err from exc
        # ``changed=False`` is reassign_task's idempotent no-op (the card
        # already had this owner). Recording it as MOVED would make the
        # undo hand it back to an owner it never had.
        if result.get("changed"):
            migration.moved.append(task_id)
        else:
            migration.skipped.append(task_id)
    return migration


def undo_migrate_cards(migration: CardMigration) -> list[str]:
    """Hand every card :func:`migrate_cards` moved back to the old owner.

    Best-effort by design: this runs on the rollback path, where raising
    would hide the ORIGINAL failure. Returns the ids it could NOT restore
    so the caller can print them — an operator who sees the list can
    finish the job with ``scitex-todo`` directly.
    """
    todo = _store_module()
    failed: list[str] = []
    for task_id in migration.moved:
        try:
            todo.reassign_task(
                migration.store,
                task_id=task_id,
                new_owner=migration.old,
                by=None,
            )
        except Exception:  # noqa: BLE001 - collect, never mask the root cause
            failed.append(task_id)
    return failed


__all__ = [
    "CardMigration",
    "CardMigrationError",
    "find_foreign_scoped_cards",
    "find_owned_cards",
    "migrate_cards",
    "undo_migrate_cards",
]
