"""Parser for ``spec.skills``."""

from __future__ import annotations

from .._types import SkillsSpec


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
    return SkillsSpec(
        required=raw.get("required", []) or [],
        available=raw.get("available", []) or [],
        injection_mode=mode,
        match_by=match_by_value,
        match_style=style,
    )
