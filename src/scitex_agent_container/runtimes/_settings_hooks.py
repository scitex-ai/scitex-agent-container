"""Claude ``hooks``-block algebra — merge and prune, as pure dict->dict.

Extracted from :mod:`.settings_json`, which had grown past the module line
limit. These three functions share one shape (walk events -> walk matcher
groups -> walk hook entries) and touch neither ``AgentConfig``, the settings
file, nor the filesystem, so they stand on their own. ``_layer_merge``
already imported one of them across module boundaries, which is the tell.

:mod:`.settings_json` re-exports all three, so existing imports through that
module keep resolving.
"""

from __future__ import annotations

import re

#: Every hook SAC OWNS and re-injects from ``_HOOKS_CONFIG`` on each
#: materialise: the event-ring ingest hooks (current ``event ingest`` and the
#: renamed-away ``ingest-hook-event``) and the never-stop actuator
#: (``take-next-item``).
#:
#: Ownership is the whole criterion. De-duplication in
#: :func:`_merge_hooks_blocks` compares WHOLE matcher-groups, so as soon as a
#: group's contents change — a command renamed, or a second hook added
#: beside an existing one — the old group is no longer equal to the new one
#: and BOTH survive. That is how the 2026-06-23 duplicate-ingest incident
#: happened, and adding the actuator to the Stop group reproduced it exactly
#: (a second materialise yielded ``take-next-item`` twice). So every
#: SAC-owned command must be pruned from the merge BASE, not just the one
#: that broke last time.
_SAC_OWNED_RE = re.compile(
    r"(?:scitex-agent-container|sac)\s+"
    r"(?:ingest-hook-event|event\s+ingest|take-next-item)\b"
)


def _merge_hooks_blocks(base: object, overlay: object) -> dict:
    """Per-event deep-merge of two Claude ``hooks`` blocks.

    Concatenates each event's matcher-groups (``base`` first, then
    ``overlay``), de-duping identical groups so repeated runs stay
    idempotent. Preserves baseline hooks (e.g. a project's honest-grounding
    Stop gate / lint PostToolUse) instead of clobbering them with the
    overlay. Non-list event values in ``overlay`` replace the base entry.
    A non-dict ``base`` is treated as empty.
    """
    merged: dict = {}
    if isinstance(base, dict):
        merged = {
            ev: list(groups) for ev, groups in base.items() if isinstance(groups, list)
        }
    if isinstance(overlay, dict):
        for ev, groups in overlay.items():
            if not isinstance(groups, list):
                merged[ev] = groups
                continue
            dest = merged.setdefault(ev, [])
            for grp in groups:
                if grp not in dest:
                    dest.append(grp)
    return merged


def _strip_stale_sac_ingest_hooks(hooks: object) -> dict:
    """Drop every SAC-OWNED hook from an existing hooks block.

    SAC owns these hooks and re-injects the CURRENT form from
    ``_HOOKS_CONFIG`` on every materialise. :func:`_merge_hooks_blocks`
    preserves baseline hooks by concatenating + de-duping IDENTICAL groups —
    but that comparison is on the whole GROUP, so any change to a group's
    contents defeats it and leaves BOTH copies in place.

    That has now bitten twice. First a rename: a stale ``scitex-agent-container
    ingest-hook-event <kind>`` survived alongside the new ``… event ingest
    <kind>``, both ran, and the deprecated form's loud shim error BLOCKED
    every UserPromptSubmit (proj-scitex-dev 2026-06-23 — the agent received
    Telegram but could not act on it). Then an addition: putting the
    never-stop actuator in the Stop group changed that group, so a second
    materialise emitted ``take-next-item`` twice — meaning two detector
    subprocesses and two loop-guard increments per turn end.

    Stripping every SAC-owned command from the merge BASE makes the block
    idempotent under both renames and additions. The name is historical; the
    criterion is OWNERSHIP, not "ingest".

    Non-SAC baseline hooks (the ``_shared`` honest-grounding Stop gate, the
    lint PostToolUse) never match the pattern and are preserved; a group that
    mixes SAC-owned + other entries keeps only its non-SAC entries.
    """
    if not isinstance(hooks, dict):
        return {}
    cleaned: dict = {}
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[ev] = groups
            continue
        kept_groups: list = []
        for grp in groups:
            hk_list = grp.get("hooks") if isinstance(grp, dict) else None
            if not isinstance(hk_list, list):
                kept_groups.append(grp)
                continue
            kept_hooks = [
                hk
                for hk in hk_list
                if not (
                    isinstance(hk, dict)
                    and isinstance(hk.get("command"), str)
                    and _SAC_OWNED_RE.search(hk["command"])
                )
            ]
            if kept_hooks:
                kept_groups.append({**grp, "hooks": kept_hooks})
            # else: a purely SAC-ingest group → drop it entirely
        if kept_groups:
            cleaned[ev] = kept_groups
    return cleaned


def _exclude_hooks(hooks: object, patterns: list[str]) -> dict:
    """Drop any hook whose command CONTAINS one of ``patterns`` (substring).

    The operator opt-out: after seeing the full materialized hook set via
    ``sac agents explain``, a spec's ``exclude_hooks`` switches specific ones
    off (e.g. ``report_to_lead_on_stop`` once the lead is retired). A group
    emptied of all hooks is removed; non-matching hooks and non-hook groups
    survive. Shares the shape of :func:`_strip_stale_sac_ingest_hooks`.
    """
    if not isinstance(hooks, dict):
        return {}
    cleaned: dict = {}
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[ev] = groups
            continue
        kept_groups: list = []
        for grp in groups:
            hk_list = grp.get("hooks") if isinstance(grp, dict) else None
            if not isinstance(hk_list, list):
                kept_groups.append(grp)
                continue
            kept = [
                hk
                for hk in hk_list
                if not (
                    isinstance(hk, dict)
                    and isinstance(hk.get("command"), str)
                    and any(p in hk["command"] for p in patterns)
                )
            ]
            if kept:
                kept_groups.append({**grp, "hooks": kept})
        if kept_groups:
            cleaned[ev] = kept_groups
    return cleaned


__all__ = [
    "_exclude_hooks",
    "_merge_hooks_blocks",
    "_strip_stale_sac_ingest_hooks",
]
