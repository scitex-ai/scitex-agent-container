"""Explicit-spec validation — every field written, or the load is RED.

WHY — operator ruling 2026-07-21 (verbatim intent, do not soften):

  1. EVERY field in an agent ``spec.yaml`` must be written explicitly.
     An omitted field is an ERROR at config-load time, with a hint.
  2. NO migration machinery, NO warn phase, NO escape-hatch env flag.
     Existing specs going boot-red at once is ACCEPTED and desired
     ("red start, hard; migration ceremony is ridiculous").
  3. The structure must leave no option but compliance — hence the only
     entry-point signature is ``validate(doc, path)``: no ``strict=``
     parameter, no bypass kwarg, nothing to turn off.

The required-key map lives in the sibling ``_explicit_fields`` (derived
from ``dataclasses.fields()`` of the section dataclasses + explicit
alias tables). This module is the WALKER: it collects ALL missing
fields in ONE pass — never fail-on-first, the hint must list everything
at once — and raises a single :class:`ExplicitSpecError` whose message
carries, per missing field, its YAML path, expected type and current
default, followed by a paste-ready YAML block that clears every error
(hint-clears-the-condition is round-trip TESTED; see the incident memory
about hints that do not clear their own gate).

Presence semantics match the pre-existing required-field checker
(operator directive 2026-06-23): a key present with a ``null`` value IS
an explicit declaration — the author wrote it; value/shape checks
belong to the section validators and parsers. Only a genuinely absent
key (or a non-mapping parent) is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ._explicit_fields import RequiredField, required_fields_for_kind

__all__ = [
    "ExplicitSpecError",
    "explicit_field_errors",
    "explicit_spec_defaults",
    "validate",
]

# Markers delimiting the paste-ready YAML block inside the error message
# so operators (and the round-trip test) can extract it verbatim.
PASTE_BEGIN = "# --- paste-ready (values = current defaults) ---"
PASTE_END = "# --- end paste-ready ---"


class ExplicitSpecError(ValueError):
    """A spec omitted required fields (red-start ruling 2026-07-21)."""


def _is_present(spec: dict, path: str) -> bool:
    """True when every level of the dotted ``path`` exists in ``spec``.

    A key present with ``None`` counts as PRESENT (explicitly declared);
    a non-mapping parent makes the leaf absent — the shape validators
    own the "must be a mapping" diagnostic.
    """
    cur: Any = spec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _missing_fields(doc: dict) -> list[RequiredField]:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        # "spec is required and must be a mapping" is validate_raw's
        # diagnostic; nothing to walk here.
        return []
    kind = doc.get("kind")
    return [
        field
        for field in required_fields_for_kind(kind)
        if not _is_present(spec, field.path)
    ]


def _paste_block(missing: list[RequiredField]) -> str:
    """Render the missing fields as one merge-ready ``spec:`` YAML doc."""
    tree: dict = {}
    for field in missing:
        cur = tree
        parts = field.path.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = field.paste_value
    return yaml.safe_dump(
        {"spec": tree}, sort_keys=False, default_flow_style=False
    ).rstrip()


def _render_message(missing: list[RequiredField], path: Path | str) -> str:
    lines = [
        f"spec is missing {len(missing)} required field(s). EVERY spec "
        "field must be written explicitly — an omitted field is a load "
        "ERROR (operator ruling 2026-07-21: red-start; no migration "
        "phase, no bypass).",
        "",
        "Missing fields (YAML path — expected type — current default):",
    ]
    lines += [
        f"  - spec.{field.path} — {field.type_str} — {field.default_repr}"
        for field in missing
    ]
    lines += [
        "",
        "Merge this block into the spec to clear every error above "
        "(values are the current defaults, so behaviour is unchanged):",
        "",
        PASTE_BEGIN,
        _paste_block(missing),
        PASTE_END,
        "",
        f"While loading: {path}",
    ]
    return "\n".join(lines)


def explicit_field_errors(doc: dict, path: Path | str) -> list[str]:
    """List-form surface for ``validate_raw`` (0 or 1 consolidated message).

    Returns an empty list when every required field is present, else a
    single-element list holding the full consolidated message — one
    error entry, everything listed at once, so the CLI ``sac agents
    check`` path and ``load_config`` report identically.
    """
    if not isinstance(doc, dict):
        return []
    missing = _missing_fields(doc)
    if not missing:
        return []
    return [_render_message(missing, path)]


def validate(doc: dict, path: Path | str) -> None:
    """Raise :class:`ExplicitSpecError` unless every field is explicit.

    The ONLY signature — no env flag, no bypass parameter, no
    ``strict=False``. Wired into ``load_v3`` BEFORE parsing so a red
    spec fails with the complete hint, not a parser TypeError.
    """
    if not isinstance(doc, dict):
        return
    missing = _missing_fields(doc)
    if missing:
        raise ExplicitSpecError(_render_message(missing, path))


def explicit_spec_defaults(kind: object = "Agent") -> dict:
    """Full ``spec`` mapping with every required field at its paste value.

    The same tree the paste-ready hint would emit for a spec missing
    everything. Fixture/tooling surface (e.g. test scaffolds and
    ``sac agents create`` templates) so repo-owned specs can never
    drift from the required map.
    """
    tree: dict = {}
    for field in required_fields_for_kind(kind):
        cur = tree
        parts = field.path.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = field.paste_value
    return tree
