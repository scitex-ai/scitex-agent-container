#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Symlink-farm -> spec.yaml group-label sync (operator authoring aid).

The operator maintains ad hoc named-group membership by placing a
symlink for each member agent under
``~/.scitex/agent-container/agents/_group_<name>/<agent> -> ../<agent>``
(e.g. ``_group_active/figrecipe -> ../figrecipe``) -- a fast,
directory-listing-friendly way to eyeball/edit a cohort without
hand-editing N YAML files by hand. This module is the ONE-DIRECTION
compiler from that convention INTO each member's
``metadata.labels.groups`` list -- the SSOT bulk-lifecycle code
(:mod:`cli_pkg.lifecycle._start_group_filter`) reads ONLY the spec
field at request time (see that module's docstring for why: one
source, no live filesystem-glob dependency on the hot start path).
Re-run the sync after editing the symlink farm to keep specs current.

``sync_groups_line`` is a pure TEXT-LINE editor, not a full YAML
round-trip: every spec.yaml in this fleet authors ``groups:`` as a
single-line flow list (``    groups: [a, b, c]``), so a targeted
regex find-and-append on THAT LINE ONLY preserves every comment,
blank line, and key order in the rest of the file -- a full
ruamel/pyyaml load+dump cycle risks subtly reformatting unrelated
content. If a spec's ``groups:`` line is not in this exact
single-line flow form, the append is refused (``changed=False``)
rather than guessing -- fail loud (surface as "needs manual
attention") beats a silent, possibly-corrupting rewrite.
"""

from __future__ import annotations

import re
from pathlib import Path

_GROUPS_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)groups:[ \t]*\[(?P<items>[^\]]*)\](?P<trail>.*)$"
)


def sync_groups_line(text: str, group: str) -> tuple[str, bool]:
    """Ensure spec YAML ``text`` lists ``group`` in its ``groups:`` line.

    Returns ``(new_text, changed)``. ``changed`` is False when the group
    is already present (idempotent no-op) or when no single-line
    flow-style ``groups: [...]`` line is found in ``text`` at all.
    Only the FIRST such line is touched (specs author exactly one).
    """
    group = group.strip()
    if not group:
        return text, False
    lines = text.splitlines(keepends=True)
    for i, raw_line in enumerate(lines):
        ending = ""
        body = raw_line
        if body.endswith("\r\n"):
            ending, body = "\r\n", body[:-2]
        elif body.endswith("\n"):
            ending, body = "\n", body[:-1]
        m = _GROUPS_LINE_RE.match(body)
        if not m:
            continue
        items = [it.strip() for it in m.group("items").split(",") if it.strip()]
        if group in items:
            return text, False
        items.append(group)
        new_body = f"{m.group('indent')}groups: [{', '.join(items)}]{m.group('trail')}"
        lines[i] = new_body + ending
        return "".join(lines), True
    return text, False


def discover_symlink_farm_groups(agents_root: Path) -> dict[str, set[str]]:
    """Return ``{agent_name: {group_name, ...}}`` from ``_group_<name>/`` dirs.

    Walks ``agents_root`` for immediate subdirectories named
    ``_group_<name>`` and, for each, every SYMLINK entry directly
    inside it is one membership: the entry's own filename is the
    agent name (the authored convention is
    ``_group_active/figrecipe -> ../figrecipe`` -- link name and
    target basename always match, so the target is not resolved).
    Non-symlink entries are ignored. Tolerant: a missing
    ``agents_root`` returns ``{}`` rather than raising.
    """
    result: dict[str, set[str]] = {}
    if not agents_root.is_dir():
        return result
    for child in sorted(agents_root.iterdir()):
        if not child.is_dir() or not child.name.startswith("_group_"):
            continue
        group_name = child.name[len("_group_") :]
        if not group_name:
            continue
        for entry in sorted(child.iterdir()):
            if not entry.is_symlink():
                continue
            result.setdefault(entry.name, set()).add(group_name)
    return result


def sync_agent_groups_from_symlink_farm(agents_root: Path) -> dict[str, list[str]]:
    """Apply the symlink-farm membership INTO every member's spec.yaml.

    For each ``(agent, group)`` pair discovered by
    :func:`discover_symlink_farm_groups`, reads
    ``<agents_root>/<agent>/spec.yaml``, applies
    :func:`sync_groups_line`, and writes back only when it changed.
    Returns ``{agent_name: [group_names_actually_added]}`` -- agents
    with nothing to add are omitted, so an empty return means the
    farm and the specs already agree. An agent listed in the farm
    whose spec.yaml is missing or has no single-line ``groups:`` flow
    list is skipped (not raised) and simply absent from the report,
    so the caller can spot it as needing manual attention.
    """
    added: dict[str, list[str]] = {}
    for agent, groups in sorted(discover_symlink_farm_groups(agents_root).items()):
        spec_path = agents_root / agent / "spec.yaml"
        if not spec_path.is_file():
            continue
        text = spec_path.read_text()
        agent_added: list[str] = []
        for group in sorted(groups):
            text, changed = sync_groups_line(text, group)
            if changed:
                agent_added.append(group)
        if agent_added:
            spec_path.write_text(text)
            added[agent] = agent_added
    return added


__all__ = [
    "discover_symlink_farm_groups",
    "sync_agent_groups_from_symlink_farm",
    "sync_groups_line",
]
