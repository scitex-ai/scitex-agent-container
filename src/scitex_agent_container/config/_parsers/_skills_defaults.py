"""Fleet-wide base default required-skills (declared ONCE, inherited by every agent).

The operator's anti-"manage 60 packages individually" principle: a skill
the fleet must always load doesn't belong in 60 per-spec
``skills.required`` lists. It belongs in this base default, which
:func:`scitex_agent_container.config._parsers._skills.parse_skills`
merges into every agent's :class:`~scitex_agent_container.config._types.SkillsSpec.required`
at parse time. Per-spec entries are appended after the base defaults;
duplicates are dropped preserving first-occurrence order so a spec
that also names a default keeps a stable position (the base-default
slot wins).

Operator directive 2026-06-11 (lead msg 087d779): seed with
``scitex-todo`` so every agent loads it at startup.

Surface
-------
* :data:`BASE_REQUIRED_SKILLS` — module-level tuple. Adding a new
  fleet-wide default is a one-line change here;
  :mod:`test__skills_defaults` pins the exact contents so accidental
  edits surface in code review.
* :func:`apply_base_required_skills` — the merge entry-point used by
  the spec parser. Pure: no I/O, no env, no Path side-effects.
  Idempotent — applying twice equals applying once.
"""

from __future__ import annotations

# Fleet-wide required skills. Declared ONCE here so every agent's spec
# parser inherits them; per-spec ``skills.required`` lists are appended
# after this base (dedup preserves first-occurrence order).
#
# Operator directive 2026-06-11 (lead msg 087d779): ``scitex-todo`` is
# required on every agent — implements the operator's "scitex-todo must
# be a REQUIRED skill for the lead AND every sac agent" rule without
# 60 per-spec edits.
BASE_REQUIRED_SKILLS: tuple[str, ...] = ("scitex-todo",)


def apply_base_required_skills(per_spec_required: list[str]) -> list[str]:
    """Merge :data:`BASE_REQUIRED_SKILLS` ahead of ``per_spec_required``.

    Returns a new list with the base defaults prepended in declared
    order, followed by the per-spec entries. Duplicates are dropped
    preserving first-occurrence order so a per-spec list that already
    names a base default keeps a stable position in the merged output
    (the base-default slot wins; the per-spec duplicate is silently
    collapsed).

    Pure; no I/O. Does not mutate ``per_spec_required``. Idempotent —
    applying the function to its own output yields the same list.

    Parameters
    ----------
    per_spec_required
        The ``required`` list as it appears in ``spec.skills`` after
        the per-spec parse (i.e. the verbatim YAML value, modulo
        the parser's None-tolerance).

    Returns
    -------
    list[str]
        Base defaults + per-spec entries, deduped, order-preserving.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for name in (*BASE_REQUIRED_SKILLS, *per_spec_required):
        if name in seen:
            continue
        seen.add(name)
        merged.append(name)
    return merged


__all__ = ["BASE_REQUIRED_SKILLS", "apply_base_required_skills"]
