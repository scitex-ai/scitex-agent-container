#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``agent_rename`` — rename an agent everywhere, atomically, or not at all.

An agent's name is written into FIVE places on disk plus six records in the
shared PostgreSQL store plus the task board. Renaming by hand means editing
all of them consistently, and the one a human misses is silent — see
``_rename_cards`` for the card orphaning that motivated this verb.

The places (:class:`._rename_plan.Layout` owns the paths):

  1. spec dir          ``<root>/agents/<name>/``
  2. spec self-refs    labels / workdir / overlay / state-db / board identity
                       (``_rename_spec.SPEC_TOUCHPOINTS``)
  3. overlay dir       ``<root>/containers/overlays/<name>/``
  4. runtime/state dir ``<root>/runtime/<name>/``  (bound at ``/state/<name>``)
  5. registry json     ``<root>/runtime/registry/<name>.json``
  6. instances         the PostgreSQL lifetime records
  7. ACL grants        the PostgreSQL ``comms_grants`` records
  8. ACL policy        the PostgreSQL ``node_comms_policy`` record
  9. comms directory   the PostgreSQL ADR-0014 ``comms_nodes`` record
 10. spawn DAG         the PostgreSQL ``lineage`` edges
 11. channel history   the PostgreSQL ``sac_channel_events`` rows
 12. task cards        scitex-todo's ``reassign_task`` (``_rename_cards``)

WHY 6 THROUGH 11 ARE SIX STEPS AND NOT SIX MORE ``NAME_COLUMNS`` PAIRS
======================================================================
Every one of them was a ``(table, column)`` pair inside ``_rename_db`` until
its table left SQLite. That module walked ``sqlite_master`` and SKIPPED a
table it could not find — deliberately, so a fleet that has never started an
agent does not block a rename — which turned each stale pair into a SILENT
NO-OP: the rename reported success having moved nothing. Worse than a crash,
every time, and differently damaging per table. So each table left with its
pair, gaining its own step, its own inverse on the undo stack, and its own
tests against the store that now holds the rows. ``_rename_db`` itself was
deleted on 2026-08-29, when the last pair went.

The diary history (``turns`` / ``errors`` / ``heartbeats``) does NOT follow a
rename, and cannot be made to by adding a call here: ``name`` is an IDENTITY
field of the heartbeats store and an ``IMMUTABLE`` data field of the turns
and errors stores, so the store REFUSES the write by design.

ATOMICITY
---------
Each step pushes its INVERSE onto an undo stack before the next runs. Any
failure unwinds the stack in reverse, leaving the agent exactly as it was.
A half-renamed agent is worse than an un-renamed one: it starts under one
name while writing to another's overlay and board slice.

The steps are ordered so the cheap, local, likely-to-fail work happens
FIRST and the board — the only step that is slow, external, and visible to
other agents — happens LAST. A rename that is going to fail therefore
fails before it touches anything shared.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from .._state.state_db_acl_policy import rename_comms_policy
from .._state.state_db_channel import (
    ChannelRename,
    rename_channel_events,
    undo_rename_channel_events,
)
from .._state.state_db_comms_nodes import rename_comms_node
from .._state.state_db_grants_rename import (
    GrantsRenameUndo,
    rename_comms_grants,
    undo_rename_comms_grants,
)
from .._state.state_db_lineage_rename import rename_lineage
from .._state.state_db_instances_rename import (
    InstancesRenameUndo,
    rename_instance_rows,
    undo_rename_instance_rows,
)
from ._rename_cards import CardMigration, CardMigrationError, find_owned_cards
from ._rename_cards import migrate_cards, undo_migrate_cards
from ._rename_plan import (
    Layout,
    Move,
    RenameError,
    RenamePlan,
    build_plan,
    preflight,
    probe_running,
)
from ._rename_spec import rewrite_spec

# Step labels — stable strings. The CLI prints them; the rollback test
# injects a failure at each in turn.
STEP_SPEC_DIR = "spec-dir"
STEP_SPEC_FILE = "spec-file"
STEP_OVERLAY_DIR = "overlay-dir"
STEP_RUNTIME_DIR = "runtime-dir"
STEP_REGISTRY = "registry"
STEP_INSTANCES = "instances"
STEP_GRANTS = "acl-grants"
STEP_ACL = "acl-policy"
STEP_DIRECTORY = "comms-directory"
STEP_LINEAGE = "lineage"
STEP_CHANNEL = "channel-history"
STEP_CARDS = "cards"
STEP_VERIFY = "verify"

STEPS: tuple[str, ...] = (
    STEP_SPEC_DIR,
    STEP_SPEC_FILE,
    STEP_OVERLAY_DIR,
    STEP_RUNTIME_DIR,
    STEP_REGISTRY,
    STEP_INSTANCES,
    STEP_GRANTS,
    STEP_ACL,
    STEP_DIRECTORY,
    STEP_LINEAGE,
    STEP_CHANNEL,
    STEP_CARDS,
    STEP_VERIFY,
)


def apply_plan(
    plan: RenamePlan,
    *,
    store: str | Path | None = None,
    by: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> None:
    """Execute ``plan``, rolling everything back on any failure.

    ``on_step`` is called with each step's label immediately BEFORE that
    step runs. The CLI passes a progress printer; the rollback test passes
    a callback that raises on a chosen step, which aborts the rename
    mid-flight with a real exception and exercises the real unwind.

    Raises:
        RenameError: The rename failed. Everything already done has been
            undone; the message names the original cause and reports any
            part of the rollback that could not itself complete.
    """
    layout = plan.layout
    old, new = plan.old, plan.new
    undo: list[tuple[str, Callable[[], None]]] = []

    def _step(label: str) -> None:
        if on_step is not None:
            on_step(label)

    try:
        # 1. Spec dir. Every step below addresses the spec at its NEW path.
        _step(STEP_SPEC_DIR)
        _move(plan.spec_move)
        undo.append((STEP_SPEC_DIR, _undo_move(plan.spec_move)))

        # 2. The spec's self-references.
        _step(STEP_SPEC_FILE)
        _rewrite_spec_file(layout.spec_file(new), old, new, undo)

        # 3/4. Overlay + runtime/state dirs.
        for label, move in (
            (STEP_OVERLAY_DIR, plan.overlay_move),
            (STEP_RUNTIME_DIR, plan.runtime_move),
        ):
            _step(label)
            if move is None:
                continue
            _move(move)
            undo.append((label, _undo_move(move)))

        # 5. Registry JSON — its `name` + `config` fields name the agent.
        _step(STEP_REGISTRY)
        if plan.registry_move is not None:
            before = plan.registry_move.src.read_text(encoding="utf-8")
            _move(plan.registry_move)
            _rewrite_registry_json(plan.registry_move.dst, old, new, layout)
            undo.append((STEP_REGISTRY, _undo_registry(plan.registry_move, before)))

        # 6. The ``instances`` lifetime records. ``id``/``host`` are the
        # identity and are untouched — a renamed agent is the SAME agent and
        # its recorded lifetimes are the same lifetimes — so this is an UPDATE
        # and its inverse is key-scoped. Missing it is the loudest of the six
        # silent no-ops: the start PREFLIGHT reads these records, so seeing
        # none under the new name it would start a SECOND copy of a live agent.
        _step(STEP_INSTANCES)
        instances_undo: InstancesRenameUndo = rename_instance_rows(old=old, new=new)
        if instances_undo.total:
            undo.append(
                (STEP_INSTANCES, lambda: undo_rename_instance_rows(instances_undo))
            )

        # 7. The cross-group send grants — the LAST pair to leave
        # ``_rename_db``. Both columns are the IDENTITY, so this is a copy +
        # retire. Missing it cuts both ways: the renamed agent silently LOSES
        # every grant an operator gave it (``check_send_acl`` asks about the
        # LIVE name), and the old name keeps an authorisation nobody owns.
        #
        # THIS STEP CAN REFUSE, like step 10: a grant landing on a REVOKED
        # record is neither taken over (that reinstates a withdrawn grant) nor
        # skipped (that drops the one being carried) — except where the
        # carried ``created_at`` proves the two are one authorisation, which
        # is what keeps renaming BACK possible. Its inverse is key-scoped.
        _step(STEP_GRANTS)
        grants_undo: GrantsRenameUndo = rename_comms_grants(old=old, new=new)
        if grants_undo.total:
            undo.append((STEP_GRANTS, lambda: undo_rename_comms_grants(grants_undo)))

        # 8. The ACL policy record. ``name`` is the record IDENTITY, so this
        # is a copy + retire and the inverse is the same verb with the
        # arguments swapped. It raises rather than degrades when PostgreSQL is
        # unreachable: half a rename is recoverable, an agent running under a
        # name no gate has a policy for is not.
        _step(STEP_ACL)
        if rename_comms_policy(old=old, new=new):
            undo.append((STEP_ACL, _undo_acl_policy(old, new)))

        # 9. The ADR-0014 comms directory — the ROUTING half of the same
        # story. ``name`` is the record IDENTITY, so this is a copy +
        # withdraw; the inverse is the same verb with the arguments swapped.
        _step(STEP_DIRECTORY)
        if rename_comms_node(old=old, new=new):
            undo.append((STEP_DIRECTORY, _undo_comms_node(old, new)))

        # 10. The spawn DAG. THIS STEP CAN REFUSE, and unlike its neighbours
        # that refusal is expected rather than exceptional: ``parent_name`` is
        # IMMUTABLE, so an edge naming ``old`` as a PARENT cannot be
        # re-pointed at all. ``rename_lineage`` raises rather than leaving or
        # hiding it — both of which would make each child a ROOT, and a root
        # may spawn — and the raise propagates into the unwind below.
        _step(STEP_LINEAGE)
        if rename_lineage(old=old, new=new):
            undo.append((STEP_LINEAGE, _undo_lineage(old, new)))

        # 11. The channel history — the quietest of the six to lose. UNLIKE
        # steps 8 and 9 this is an in-place UPDATE: ``target`` is half of a
        # composite key, not the record identity, so the rows keep their ids
        # (and a live consumer's ``Last-Event-ID`` keeps resolving) unless the
        # destination name already owns rows. The inverse is id-scoped.
        _step(STEP_CHANNEL)
        channel_undo: ChannelRename | None = rename_channel_events(old=old, new=new)
        if channel_undo is not None:
            undo.append(
                (STEP_CHANNEL, _undo_channel_history(channel_undo)),
            )

        # 12. The board. LAST — see the module docstring.
        _step(STEP_CARDS)
        if plan.cards_enabled:
            _migrate_cards_step(old, new, store, by, undo)

        # 13. Postcondition. "I ran the steps" is not the same claim as "the
        # world is now correct", and this verb exists because the second one
        # is what an operator actually needs. Anything still standing under
        # the old name means a step silently did not take — roll back rather
        # than hand back a half-rename.
        _step(STEP_VERIFY)
        _verify_nothing_left_behind(plan, store=store)

    except BaseException as exc:
        failures = _unwind(undo)
        raise RenameError(_rollback_message(old, new, exc, failures)) from exc


def _migrate_cards_step(
    old: str,
    new: str,
    store: str | Path | None,
    by: str | None,
    undo: list[tuple[str, Callable[[], None]]],
) -> None:
    """Migrate the cards, registering the undo even on a PARTIAL failure.

    ``migrate_cards`` can fail on card 81 of 158, having already moved 80.
    Those 80 are real, committed writes on the shared board: if we let the
    exception past without registering their inverse, the unwind would
    restore the filesystem and leave 80 cards pointing at an agent that no
    longer exists. The partial migration rides on the exception for exactly
    this reason.
    """
    try:
        migration = migrate_cards(old, new, store=store, by=by)
    except CardMigrationError as exc:
        partial = getattr(exc, "migration", None)
        if partial is not None and partial.moved:
            undo.append((STEP_CARDS, _undo_cards(partial)))
        raise
    undo.append((STEP_CARDS, _undo_cards(migration)))


def _verify_nothing_left_behind(
    plan: RenamePlan, *, store: str | Path | None
) -> None:
    """Assert the old name owns nothing any more. Raises if it does."""
    layout = plan.layout
    old = plan.old
    leftovers: list[str] = []

    for label, path in (
        ("spec dir", layout.spec_dir(old)),
        ("overlay dir", layout.overlay_dir(old)),
        ("runtime dir", layout.runtime_dir(old)),
        ("registry entry", layout.registry_json(old)),
    ):
        if path.exists():
            leftovers.append(f"{label} still at {path}")

    if plan.cards_enabled:
        orphans = find_owned_cards(old, store=store)
        if orphans:
            leftovers.append(
                f"{len(orphans)} card(s) still owned by {old!r}: "
                f"{', '.join(orphans[:5])}"
            )

    if leftovers:
        raise RenameError(
            "post-rename check failed — these still belong to the OLD name:\n"
            + "\n".join(f"      - {item}" for item in leftovers)
        )


def agent_rename(
    old: str,
    new: str,
    *,
    layout: Layout | None = None,
    store: str | Path | None = None,
    cards: bool = True,
    by: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> RenamePlan:
    """Rename ``old`` to ``new`` everywhere. Returns the plan that was applied."""
    plan = build_plan(old, new, layout=layout, store=store, cards=cards)
    apply_plan(plan, store=store, by=by, on_step=on_step)
    return plan


# ---------------------------------------------------------------------------
# Steps + their inverses
# ---------------------------------------------------------------------------


def _move(move: Move) -> None:
    move.dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(move.src), str(move.dst))


def _undo_move(move: Move) -> Callable[[], None]:
    return lambda: _move(Move(move.dst, move.src))


def _undo_acl_policy(old: str, new: str) -> Callable[[], None]:
    """Put the policy record back under ``old``. Same verb, arguments swapped.

    Legitimate here (and for the directory below) because ``name`` is the
    whole identity: the reversed call copies the values back and retires the
    name the forward step created, leaving exactly one live record under the
    name the agent actually has. The channel, instances and grants steps
    CANNOT be inverted this way — see :func:`_undo_channel_history`.
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
    """Put the directory entry back under ``old``. Same verb, arguments swapped."""

    def _undo() -> None:
        rename_comms_node(old=new, new=old)

    return _undo


def _undo_channel_history(undo: ChannelRename) -> Callable[[], None]:
    """The inverse of the channel step — id-scoped, not the same verb reversed.

    ``rename_channel_events(old=new, new=old)`` would look symmetric and be
    wrong: it would also drag rows that legitimately held ``new`` BEFORE the
    rename (the leftovers of a previously deleted agent by that name) over to
    ``old``. Recording what was touched is what makes an undo exact — the
    property the deleted ``_rename_db`` bought with its rowid capture, and the
    one the instances and grants steps reproduce with store keys.
    """

    def _undo() -> None:
        undo_rename_channel_events(undo)

    return _undo


def _rewrite_spec_file(
    spec_path: Path,
    old: str,
    new: str,
    undo: list[tuple[str, Callable[[], None]]],
) -> None:
    """Rewrite the spec in place, then prove it still validates.

    The undo is pushed BEFORE the validation runs, so a spec that the
    rewrite broke is restored by the unwind rather than left on disk.
    """
    original = spec_path.read_text(encoding="utf-8")
    rewritten, _changes = rewrite_spec(original, old, new)
    if rewritten == original:
        return
    spec_path.write_text(rewritten, encoding="utf-8")
    undo.append(
        (STEP_SPEC_FILE, lambda: spec_path.write_text(original, encoding="utf-8"))
    )

    from ..config import validate_config

    errors = validate_config(str(spec_path))
    if errors:
        raise RenameError(
            f"the rewritten spec at {spec_path} no longer validates:\n"
            + "\n".join(f"      - {e}" for e in errors)
        )


def _rewrite_registry_json(path: Path, old: str, new: str, layout: Layout) -> None:
    """Point the moved registry entry's ``name`` + ``config`` at the new agent."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenameError(f"registry entry {path} is unreadable: {exc}") from exc
    if entry.get("name") == old:
        entry["name"] = new
    if entry.get("config", "") == str(layout.spec_file(old)):
        entry["config"] = str(layout.spec_file(new))
    path.write_text(json.dumps(entry, indent=2), encoding="utf-8")


def _undo_registry(move: Move, before: str) -> Callable[[], None]:
    def _undo() -> None:
        _move(Move(move.dst, move.src))
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


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


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


__all__ = [
    "STEPS",
    "STEP_ACL",
    "STEP_CARDS",
    "STEP_CHANNEL",
    "STEP_DIRECTORY",
    "STEP_GRANTS",
    "STEP_INSTANCES",
    "STEP_LINEAGE",
    "STEP_OVERLAY_DIR",
    "STEP_REGISTRY",
    "STEP_RUNTIME_DIR",
    "STEP_SPEC_DIR",
    "STEP_SPEC_FILE",
    "STEP_VERIFY",
    "Layout",
    "Move",
    "RenameError",
    "RenamePlan",
    "agent_rename",
    "apply_plan",
    "build_plan",
    "preflight",
    "probe_running",
]
