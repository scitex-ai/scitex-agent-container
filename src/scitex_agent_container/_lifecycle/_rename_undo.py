#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_lifecycle/_rename_undo.py
"""The inverse of every rename step, and the unwinder that runs them.

Split out of :mod:`._rename` under the per-file line cap. The seam is not
arbitrary: that module held the forward orchestration AND the inverse of every
step, so each store migration grew it TWICE — ``comms_nodes``,
``node_comms_policy``, ``instances``, ``lineage`` and ``channel_events`` each
added a step and an undo. Splitting along that seam is what stops the next
migration re-creating the overflow.

WHAT THESE SHARE, and it is a contract rather than a naming convention: each
returns a ``Callable[[], None]`` recorded by ``apply_plan`` at the moment its
forward step SUCCEEDED, and :func:`_unwind` is the only caller. An inverse is
therefore never speculative — it runs only against a change known to have
landed.

TWO OF THEM ARE NOT "THE SAME VERB REVERSED", deliberately.
:func:`_undo_channel_history` is id-scoped, because winding the event-id
counter back would hand a live consumer's ``Last-Event-ID`` to a different
event. :func:`_undo_cards` re-runs a migration rather than inverting a write.
Both are noted at their definitions; the distinction is the reason this file
cannot be generated.
"""

from __future__ import annotations

import shutil  # noqa: F401 - kept for parity with the moved helpers
from pathlib import Path  # noqa: F401 - used in annotations below
from typing import Callable

from .._state.state_db_acl_policy import rename_comms_policy
from .._state.state_db_channel import ChannelRename, undo_rename_channel_events
from .._state.state_db_comms_nodes import rename_comms_node
from .._state.state_db_grants_rename import (
    GrantsRenameUndo,
    undo_rename_grant_rows,
)
from .._state.state_db_lineage_rename import rename_lineage
from ._rename_cards import CardMigration, CardMigrationError, undo_migrate_cards
from ._rename_plan import Move, move_path

__all__ = [
    "_rollback_message",
    "_undo_acl_policy",
    "_undo_cards",
    "_undo_channel_history",
    "_undo_comms_node",
    "_undo_grants",
    "_undo_lineage",
    "_undo_move",
    "_undo_registry",
    "_unwind",
]


def _undo_move(move: Move) -> Callable[[], None]:
    return lambda: move_path(Move(move.dst, move.src))


def _undo_acl_policy(old: str, new: str) -> Callable[[], None]:
    """Put the policy record back under ``old``.

    The same verb with the arguments swapped — it copies the values back and
    retires the name the forward step created, so an unwound rename leaves
    exactly one live policy, under the name the agent actually has.
    """

    def _undo() -> None:
        rename_comms_policy(old=new, new=old)

    return _undo


def _undo_lineage(old: str, new: str) -> Callable[[], None]:
    """Move the lineage edge back. The same verb, arguments swapped.

    Cannot itself hit the parent-side refusal: this only runs when the
    forward call SUCCEEDED, which means nothing named ``old`` as a parent,
    and the forward call did not create such an edge.
    """

    def _undo() -> None:
        rename_lineage(old=new, new=old)

    return _undo


def _undo_comms_node(old: str, new: str) -> Callable[[], None]:
    """Put the directory entry back under ``old``.

    The same verb with the arguments swapped — it copies the routing tuple
    back and withdraws the name the forward step created, so an unwound
    rename leaves exactly one live entry, under the name the agent actually
    answers to.
    """

    def _undo() -> None:
        rename_comms_node(old=new, new=old)

    return _undo


def _undo_channel_history(undo: ChannelRename) -> Callable[[], None]:
    """The inverse of step 9 — id-scoped, not the same verb reversed.

    ``rename_channel_events(old=new, new=old)`` would look symmetric and be
    wrong: it would also drag rows that legitimately held ``new`` BEFORE the
    rename (the leftovers of a previously deleted agent by that name) over to
    ``old``. The recorded ids are what make the undo exact, the same property
    ``_rename_db``'s rowid capture buys for the SQLite half.
    """

    def _undo() -> None:
        undo_rename_channel_events(undo)

    return _undo


def _undo_registry(move: Move, before: str) -> Callable[[], None]:
    def _undo() -> None:
        move_path(Move(move.dst, move.src))
        move.src.write_text(before, encoding="utf-8")

    return _undo


def _undo_cards(migration: CardMigration) -> Callable[[], None]:
    def _undo() -> None:
        failed = undo_migrate_cards(migration)
        if failed:
            raise CardMigrationError(
                f"could not hand these cards back to {migration.old!r}: "
                f"{', '.join(failed)}"
            )

    return _undo


def _unwind(undo: list[tuple[str, Callable[[], None]]]) -> list[str]:
    """Run every recorded inverse, newest first. Never raises."""
    failures: list[str] = []
    for label, revert in reversed(undo):
        try:
            revert()
        except Exception as exc:  # noqa: BLE001 - collect; never mask the cause
            failures.append(f"{label}: {exc}")
    return failures


def _rollback_message(
    old: str, new: str, exc: BaseException, failures: list[str]
) -> str:
    head = f"rename {old!r} -> {new!r} FAILED at: {exc}"
    if not failures:
        return f"{head}\n    Rolled back — {old!r} is exactly as it was."
    listed = "\n".join(f"      - {f}" for f in failures)
    return (
        f"{head}\n"
        f"    ROLLBACK INCOMPLETE. These could not be undone:\n{listed}\n"
        f"    {old!r} is in a PARTIAL state — fix the above by hand before "
        "starting either name."
    )


def _undo_grants(undo: GrantsRenameUndo) -> Callable[[], None]:
    """Put every carried grant back, BY KEY rather than by re-running.

    Not "the same verb with the arguments swapped", which is what
    :func:`_undo_acl_policy` and :func:`_undo_comms_node` can afford. A grant
    is not exclusive, so ``new`` may legitimately have held grants BEFORE the
    rename; running the rename backwards would withdraw those too. The forward
    step therefore records the exact identities it created, hid and un-hid, and
    this restores only those.
    """

    def _undo() -> None:
        undo_rename_grant_rows(undo)

    return _undo


# EOF
