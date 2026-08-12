"""Build a :class:`MigrationPlan` by running a line editor over every spec.

:mod:`._layers_migration_model` describes what a sweep WOULD do and
:mod:`._layers_migration_apply` applies it transactionally, but nothing ever
built the plan that connects them — ``plan_migration``, named in the model's
own docstring, does not exist, and no production code constructs a
``MigrationPlan``. The kit has a front and a back and no middle.

This is the middle, and it is written once for ALL spec sweeps rather than per
migration. The per-migration variation is exactly one thing — which text edit
to attempt — so it is a parameter (``edit_fn``), not a copy of this file. The
``to_home_layers`` sweep can adopt it unchanged.

What it refuses to blur, inherited from the model:

* A spec the editor declines is REFUSED and NAMED, never silently skipped.
  Skipping is how a sweep reports "101 done" over a fleet of 102.
* A spec that cannot be READ is ``unreadable``, which is a different thing
  from an edit that was declined, and it makes the plan unsafe.
* A planned edit touching other than one line is MALFORMED — a defect in the
  editor, not one more spec needing attention.

:func:`group_refusals` exists because of a fact peculiar to a nearly-complete
migration: 101 of 102 specs already satisfy the a2a sweep, so the plan's own
``summary()`` would print 101 agent names and bury the one refusal that
actually needs a human. Grouping by REASON puts "already declared: 101" on one
line and leaves anything unrecognised standing alone where it can be seen.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ._layers_migration_model import MigrationPlan, SpecEdit, count_added_lines


def fleet_spec_paths(agents_root: Path) -> "list[Path]":
    """Every ``<agent>/spec.yaml`` under ``agents_root``, in a stable order.

    Directories starting with ``_`` (``_shared``, ``_template_*``) are
    scaffolding, not fleet agents, and are skipped — the same rule
    ``cli_pkg.refresh_acl._fleet_spec_paths`` applies when it enumerates the
    fleet. Sorted so a dry-run and the apply that follows it agree on order.
    """
    return sorted(
        p for p in agents_root.glob("*/spec.yaml") if not p.parent.name.startswith("_")
    )


def plan_spec_sweep(agents_root: Path, edit_fn: "callable") -> MigrationPlan:
    """Run ``edit_fn`` over every fleet spec and collect the result. NO writes.

    ``edit_fn`` takes the spec's text and returns an object with ``.text``,
    ``.changed`` and ``.reason`` — the :class:`..config._a2a_host_line.LineEdit`
    shape. ``reason`` is what makes a 102-spec dry-run readable; an editor that
    returns only a boolean can still be used, but every refusal then lands in
    one anonymous bucket.

    Reading is the only I/O. The returned plan is a value, and it is what
    :func:`.._layers_migration_apply.apply_migration` consumes.
    """
    edits: list[SpecEdit] = []
    unreadable: list[str] = []

    for path in fleet_spec_paths(agents_root):
        agent = path.parent.name
        try:
            before = path.read_text()
        except (
            OSError,
            UnicodeDecodeError,
        ) as exc:  # stx-allow: fallback (reason: one unreadable spec must not abort the sweep; it is recorded and makes the plan unsafe)
            # NOT a refusal. A refusal means the editor looked and declined;
            # this means we never got to look, and the plan therefore does not
            # describe what would happen. `safe_to_apply` treats it as fatal.
            unreadable.append(f"{agent}: {exc}")
            continue

        outcome = edit_fn(before)
        if not outcome.changed:
            edits.append(
                SpecEdit(
                    agent=agent,
                    path=path,
                    layers=(),
                    refusal=getattr(outcome, "reason", None) or "declined by editor",
                )
            )
            continue

        edits.append(
            SpecEdit(
                agent=agent,
                path=path,
                layers=(),
                new_text=outcome.text,
                lines_added=count_added_lines(before, outcome.text),
            )
        )

    return MigrationPlan(edits=tuple(edits), unreadable=tuple(unreadable))


def group_refusals(plan: MigrationPlan) -> "dict[str, tuple[str, ...]]":
    """Map each distinct refusal reason to the agents that gave it.

    Returned sorted by reason, agents sorted within each reason, so two runs
    over an unchanged fleet print identically and a diff of two dry-runs shows
    only real movement.
    """
    grouped: "dict[str, list[str]]" = defaultdict(list)
    for edit in plan.refused:
        grouped[edit.refusal or ""].append(edit.agent)
    return {reason: tuple(sorted(grouped[reason])) for reason in sorted(grouped)}


__all__ = ["fleet_spec_paths", "group_refusals", "plan_spec_sweep"]
