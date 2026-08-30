#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_grants_rename.py
"""Carry an agent's cross-group grants onto a new name — or refuse.

The grants half of the agent-rename flow. It replaces the two entries
``_lifecycle/_rename_db.NAME_COLUMNS`` carried until 2026-08-29,
``("comms_grants", "sender_name")`` and ``("comms_grants", "target_name")``,
which renamed the rows with an ``UPDATE`` over a table that stopped
existing on 2026-08-28.

Leaving those pairs behind was not an option, for the reason every table
before this one had its pairs removed: ``rename_rows`` SKIPPED a table absent
from the schema, so a stale pair did not crash — it reported SUCCESS
while the grants stayed under the old name. For an ACL table that silence
cuts both ways and both ways are wrong:

  * the renamed agent LOSES every grant it had. ``check_send_acl`` reads
    ``has_grant(sender=<live name>, ...)``, finds nothing, and denies a send
    the operator explicitly authorised — a quiet, permanent revocation
    nobody ordered;
  * the OLD name keeps a live grant. A standing authorisation for a name no
    agent answers to is an ACL row nobody owns, and it becomes an
    escalation the moment a future agent is created under that name.

BOTH FIELDS ARE THE IDENTITY, SO A RENAME IS A COPY AND A RETIRE
================================================================
``sender_name`` and ``target_name`` are both IDENTITY fields, exactly as the
original ``(sender_name, target_name)`` lookup treated them. Rewriting either
is therefore not an update: it is one record ending and another beginning —
the :func:`.state_db_acl_policy.rename_comms_policy` shape. A grant may
match on EITHER side (the renamed agent as sender, as target, or a
self-grant on both), and every matching record moves.

``created_at`` TRAVELS VERBATIM. The permission was given when it was given,
and a rename is not a re-grant; the field is IMMUTABLE besides, so a
re-stamp would be a lie the merge would reject anyway.

WHY A HIDDEN RECORD AT THE DESTINATION IS USUALLY A REFUSAL
============================================================
:func:`.revoke_send` HIDES rather than deletes, so a hidden
``(new_name, X)`` record is not debris — it is normally a REVOKE somebody
performed on purpose, and the store keeps it precisely so that decision
stays auditable. Renaming ``old`` onto ``new`` when such a record stands
leaves two answers and neither is defensible:

  * take the identity over (``unhide`` + ``put``, which is what
    :func:`.rename_comms_node` does for ANY withdrawn directory entry) —
    that silently REINSTATES a revoked authorisation as a side effect of a
    rename. An ACL grant coming back from the dead is exactly the class of
    change that must be deliberate;
  * skip that one record — the renamed agent silently loses that grant,
    which is the same quiet revocation this module exists to prevent, just
    reached by a different route.

So it REFUSES, before writing anything, naming every blocked pair.
:exc:`GrantsRenameError` propagates through :mod:`.._lifecycle._rename`,
which unwinds the whole rename. The operator's remedy is to decide what the
revoked grant should be — re-grant it or revoke the source — and then
rename. The directory store makes the opposite call for its own withdrawn
records, and the difference is real: a withdrawn ROUTING entry is a stale
address, while a hidden GRANT is a standing security decision.

THE ONE EXCEPTION, AND WHY IT IS NOT A HOLE
--------------------------------------------
A rename BACK — ``old`` → ``new`` → ``old`` — meets a hidden record at its
destination that no operator revoked: the FORWARD rename retired it. Refusing
there would make ``sac agents rename`` a one-way door for any agent that
holds a grant, which is precisely what :func:`.rename_comms_node` refuses to
let happen to itself ("refusing here would make the documented inverse
impossible").

The two cases are distinguishable, and the field that distinguishes them is
the one this module already carries VERBATIM. A record retired by an earlier
rename shares its ``created_at`` with the record that replaced it, because
the carry copies that stamp rather than re-stamping. A revoked grant that
merely happens to sit at the destination was granted at its own moment and
carries its own. So the destination is revived ONLY when its ``created_at``
is exactly the one being carried onto it — evidence that the two records are
the same authorisation, one descended from the other — and refused
otherwise. Forging that match needs write access to the grants store, which
is already the authority the refusal protects.

A LIVE record at the destination is NOT a refusal either. ``new`` already
being authorised to send to ``X`` is the outcome the carry was trying to
produce, and ``created_at`` is IMMUTABLE so writing over it would change
nothing. The source is retired and the destination is left exactly as it was
— including its own, older, ``created_at``.

THE UNDO IS KEY-SCOPED, NOT THE SAME VERB REVERSED
===================================================
"Call it again with the arguments swapped" is the inverse for
``rename_comms_policy`` and ``rename_comms_node``. It is WRONG here, and not
subtly: the forward step hides the source records, so the reverse call would
see hidden records sitting at ITS destination and refuse — the undo could
never run. Recording the exact identities touched and inverting key by key
is also what stops the unwind from disturbing a record that legitimately
held the new name before the rename began.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .state_db_grants_store import ACTOR, grant_key, run_with_reconnect

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "GrantsRenameError",
    "GrantsRenameUndo",
    "count_grant_rename_rows",
    "rename_comms_grants",
    "undo_rename_comms_grants",
]


class GrantsRenameError(RuntimeError):
    """A grant could not follow the rename. NOTHING was changed.

    Raised before any write, so the store is exactly as it was and the
    caller's unwind has nothing to undo for this step.
    """


@dataclass
class GrantsRenameUndo:
    """Key-scoped inverse of a completed :func:`rename_comms_grants`.

    Records the exact identities touched BEFORE touching them — the
    discipline ``_lifecycle/_rename_db`` applied with rowids and
    :class:`.InstancesRenameUndo` applies with store keys.
    """

    old: str
    new: str
    #: identities this step CREATED under the new name
    created: list[dict] = field(default_factory=list)
    #: identities this step RETIRED under the old name
    retired: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.retired)


def _pair(row: Any) -> tuple[str, str]:
    """The ``(sender, target)`` identity of one stored row, as a tuple."""
    return (str(row.values["sender_name"]), str(row.values["target_name"]))


def _key_pair(key: dict) -> tuple[str, str]:
    """The same tuple, from an identity dict."""
    return (key["sender_name"], key["target_name"])


def _mapped(key: dict, old: str, new: str) -> dict:
    """``key`` with every ``old`` side replaced by ``new``.

    Both sides are checked because a grant can name the renamed agent as the
    sender, as the target, or — for a self-grant — as both.
    """
    return {
        "sender_name": new if key["sender_name"] == old else key["sender_name"],
        "target_name": new if key["target_name"] == old else key["target_name"],
    }


def _blocked(pairs: list[tuple[dict, dict]], old: str, new: str) -> str:
    """The refusal message, naming every pair that cannot be carried."""
    listed = ", ".join(
        f"{src['sender_name']} -> {src['target_name']} "
        f"(would become {dst['sender_name']} -> {dst['target_name']})"
        for src, dst in pairs
    )
    return (
        f"cannot rename {old!r} to {new!r}: {len(pairs)} comms_grants "
        f"record(s) would land on a REVOKED grant ({listed}). That record is "
        f"a revoke somebody performed deliberately — its ``created_at`` is "
        f"its own, so it did not come from an earlier rename of this grant. "
        f"Taking the identity over would silently reinstate it, and skipping "
        f"the record would silently drop the grant being carried. Nothing was "
        f"changed. Decide what the revoked grant should be first (re-grant "
        f"it, or revoke the {old!r} side), then rename."
    )


def rename_comms_grants(*, old: str, new: str) -> GrantsRenameUndo:
    """Move every grant naming ``old`` onto ``new``. Returns its inverse.

    Two passes, and the order is the safety property:

    1. REFUSE FIRST. If any destination identity is occupied by a HIDDEN
       record whose ``created_at`` is NOT the one being carried onto it,
       raise :exc:`GrantsRenameError` naming those pairs. Nothing is written
       — see the module docstring for why neither silent answer is
       acceptable, and why a MATCHING stamp is the rename-back case rather
       than a revoke.
    2. CARRY. For each live record naming ``old``, write the mapped identity
       with ``created_at`` and ``note`` copied VERBATIM, then retire the
       source. A destination that is already LIVE is left alone and only the
       source is retired: it already grants what the carry was for. A
       destination hidden by an earlier rename of this same grant is REVIVED
       rather than rewritten — the values are already the ones this pass
       would have written.

    Idempotent in the useful sense: with nothing live under ``old`` it moves
    nothing and returns an empty undo, so a re-run after a partial rename
    does not disturb the records already sitting under ``new``.
    """
    undo = GrantsRenameUndo(old=old, new=new)
    if not old or not new or old == new:
        return undo

    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    def _move(store: "Store") -> GrantsRenameUndo:
        # ONE read for both passes. ``include_hidden`` is what makes the
        # refusal possible at all: a revoked destination is invisible to a
        # default read, so scanning without it would walk straight into the
        # silent-reinstatement case.
        rows = store.rows(include_hidden=True)
        hidden = {_pair(row): float(row.values["created_at"]) for row in rows
                  if row.hidden}
        live = {_pair(row) for row in rows if not row.hidden}

        sources = sorted(
            (row for row in rows if not row.hidden and old in _pair(row)),
            key=_pair,
        )
        moves = [(grant_key(row.values), _mapped(grant_key(row.values), old, new))
                 for row in sources]

        # A hidden destination is a REVOKE unless its stamp is the one being
        # carried onto it, in which case it is this very grant retired by an
        # earlier rename. See the module docstring for why that distinction is
        # sound and why refusing both would make a rename a one-way door.
        refused = [
            (src, dst)
            for row, (src, dst) in zip(sources, moves)
            if _key_pair(dst) in hidden
            and hidden[_key_pair(dst)] != float(row.values["created_at"])
        ]
        if refused:
            raise GrantsRenameError(_blocked(refused, old, new))

        for row, (src, dst) in zip(sources, moves):
            if _key_pair(dst) in hidden:
                store.unhide(dst, expected_revision=ANY_REVISION, actor=ACTOR)
                live.add(_key_pair(dst))
                del hidden[_key_pair(dst)]
                undo.created.append(dst)
            elif _key_pair(dst) not in live:
                store.put(
                    {
                        **dst,
                        # Verbatim. The permission was granted when it was
                        # granted; the field is IMMUTABLE besides.
                        "created_at": float(row.values["created_at"]),
                        "note": row.values.get("note"),
                    },
                    expected_revision=NEW_RECORD,
                    actor=ACTOR,
                )
                live.add(_key_pair(dst))
                undo.created.append(dst)
            store.hide(src, expected_revision=ANY_REVISION, actor=ACTOR)
            undo.retired.append(src)
        return undo

    return run_with_reconnect(_move)


def undo_rename_comms_grants(undo: GrantsRenameUndo) -> None:
    """Restore every record :func:`rename_comms_grants` touched, by key.

    RETRACT BEFORE RESTORE. The destinations this step created are hidden
    FIRST and the sources unhidden second, so the window between the two
    authorises NOTHING. An ACL unwind that briefly authorised both names
    would be the one ordering with a failure mode.
    """
    if undo.total == 0:
        return

    from scitex_dev.store import ANY_REVISION

    def _restore(store: "Store") -> None:
        for key in undo.created:
            store.hide(key, expected_revision=ANY_REVISION, actor=ACTOR)
        for key in undo.retired:
            store.unhide(key, expected_revision=ANY_REVISION, actor=ACTOR)

    run_with_reconnect(_restore)


def count_grant_rename_rows(*, old: str) -> dict[str, int]:
    """``{"comms_grants.<field>": n}`` for what a rename would carry.

    READ-ONLY, and reported under the same ``table.column`` keys the rest of
    the dry-run report uses — the keys the two deleted ``NAME_COLUMNS`` pairs
    printed under, so an operator who has run this before reads the same
    list. A self-grant counts once on each side, exactly as two
    ``WHERE`` clauses over one row would have counted it.
    """
    if not old:
        return {}
    counts = {"sender_name": 0, "target_name": 0}

    def _count(store: "Store") -> None:
        for row in store.rows():
            if row.values["sender_name"] == old:
                counts["sender_name"] += 1
            if row.values["target_name"] == old:
                counts["target_name"] += 1

    run_with_reconnect(_count)
    return {f"comms_grants.{k}": v for k, v in counts.items() if v}

# EOF
