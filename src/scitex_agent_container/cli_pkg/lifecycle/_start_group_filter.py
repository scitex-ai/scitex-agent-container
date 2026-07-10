#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``--group`` bulk-target resolution for ``sac agents start``.

Split out of ``_start.py`` to keep the click entry point under the
per-file line cap (operator ask 2026-07-10: bulk-start agents by
group membership -- ``sac agents start --group developer``).

Reuses the SAME on-disk agent discovery ``sac agents list`` already
trusts (:func:`cli_pkg._helpers._discover_defined_agents`) and the
SSOT multi-value group reader
(:func:`config._group_resolver.all_named_groups`) rather than
re-deriving either. Deliberately reads ONLY each spec's
``metadata.labels.groups`` / ``.group`` -- NOT the ``_group_<name>/``
symlink-farm authoring convention some agent roots also carry (an
operator-side editing aid synced INTO the spec by a separate one-off
sync step, not read live here). One source at request time keeps
this fast (no extra directory walk per invocation) and keeps the
spec the one place bulk-lifecycle code trusts.
"""

from __future__ import annotations

import sys

import click

group_option = click.option(
    "--group",
    "groups",
    type=str,
    multiple=True,
    default=(),
    metavar="NAME",
    help=(
        "Bulk-start every agent whose spec names this group "
        "(metadata.labels.groups / singular .group) -- e.g. "
        "--group developer starts every developer-group agent in one "
        "command. Repeatable: --group developer --group researcher "
        "unions both. Merges with any explicit TARGETS (union, "
        "de-duplicated); TARGETS may be omitted entirely when --group "
        "resolves to at least one agent. Fails loud (exit 2) if the "
        "union of all --group values matches zero agents."
    ),
)


def resolve_group_targets(wanted: tuple[str, ...]) -> list[str]:
    """Return every defined agent's name whose spec groups include ANY of ``wanted``.

    ``wanted`` is matched case-insensitively / whitespace-trimmed against
    :func:`config._group_resolver.all_named_groups` (the MULTI-value
    reader) -- e.g. ``resolve_group_targets(("developer",))`` returns
    every agent whose spec lists ``developer`` ANYWHERE in its groups,
    not just the ACL-effective first element
    (:func:`config._group_resolver.group_from_labels` answers a
    DIFFERENT question -- a2a mesh permission -- and is untouched here).

    Returns a sorted, de-duplicated list; empty when ``wanted`` is empty
    or matches no agent. Tolerant of a broken/unreadable spec -- that
    agent is simply excluded (mirrors ``get_agent_list_data``), so one
    bad yaml doesn't block a bulk start of the rest.
    """
    normalized_wanted = {w.strip().lower() for w in wanted if w and w.strip()}
    if not normalized_wanted:
        return []

    from .._helpers import _discover_defined_agents
    from ...config import load_config
    from ...config._group_resolver import all_named_groups

    matched: set[str] = set()
    for name, spec_path in _discover_defined_agents():
        # stx-allow: fallback (reason: one broken/unreadable spec must
        # not block a bulk --group start of the other, healthy agents;
        # it is simply excluded from every group)
        try:
            cfg = load_config(str(spec_path))
        except Exception:
            continue
        agent_groups = {g.strip().lower() for g in all_named_groups(cfg.labels)}
        if agent_groups & normalized_wanted:
            matched.add(name)
    return sorted(matched)


def apply_group_targets(
    targets: tuple[str, ...], groups: tuple[str, ...]
) -> tuple[str, ...]:
    """Validate + merge ``--group``-resolved names into ``targets``.

    Exits (``sys.exit(2)``) exactly like the removed ``required=True``
    click constraint did when BOTH ``targets`` and ``groups`` are empty,
    and additionally when ``groups`` is non-empty but resolves to zero
    agents (a likely typo -- never silently no-op, per operator ask).
    On success returns the union of ``targets`` and the group-resolved
    names, de-duplicated, explicit targets first (stable ordering for
    --json / logs).
    """
    if not targets and not groups:
        click.echo(
            "Error: missing TARGETS (or pass --group NAME to bulk-start "
            "by group membership).",
            err=True,
        )
        sys.exit(2)
    if not groups:
        return targets
    resolved = resolve_group_targets(groups)
    if not resolved:
        click.echo(
            f"Error: --group matched no agents for: {', '.join(groups)}",
            err=True,
        )
        sys.exit(2)
    return tuple(dict.fromkeys((*targets, *resolved)))


__all__ = ["apply_group_targets", "group_option", "resolve_group_targets"]
