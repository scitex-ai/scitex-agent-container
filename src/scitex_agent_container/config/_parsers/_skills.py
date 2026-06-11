"""Parser for ``spec.skills``.

In addition to passing the raw spec fields through with the documented
defaults, the parser merges the fleet-wide
:data:`._skills_defaults.BASE_REQUIRED_SKILLS` ahead of the per-spec
``required`` list so a skill the operator declared ONCE (e.g.
``scitex-todo``) is inherited by every agent at startup without
touching 60 per-spec YAMLs. See :mod:`._skills_defaults` for the
rationale and the merge semantics.
"""

from __future__ import annotations

from .._types import SkillsSpec
from ._skills_defaults import apply_base_required_skills


def parse_skills(spec: dict) -> SkillsSpec:
    raw = spec.get("skills", {}) or {}
    mode = (raw.get("injection_mode") or "at-import").strip()
    if mode not in {"block", "at-import"}:
        mode = "at-import"
    valid_strategies = {"skill-id", "tag", "filename"}
    match_by = raw.get("match_by")
    if match_by is None:
        match_by_value = ["skill-id", "tag"]
    else:
        match_by_value = [s for s in match_by if s in valid_strategies]
        if not match_by_value:
            match_by_value = ["skill-id", "tag"]
    style = (raw.get("match_style") or "exact").strip()
    if style not in {"exact", "partial"}:
        style = "exact"
    # Merge the fleet-wide base defaults (e.g. ``scitex-todo``) ahead of
    # the per-spec ``required`` list. Dedup-preserving-order so a spec
    # that also names the default does not get a duplicate emission in
    # the downstream CLAUDE.md ``Required Skills`` block.
    per_spec_required = raw.get("required", []) or []
    merged_required = apply_base_required_skills(list(per_spec_required))
    return SkillsSpec(
        required=merged_required,
        available=raw.get("available", []) or [],
        injection_mode=mode,
        match_by=match_by_value,
        match_style=style,
    )
