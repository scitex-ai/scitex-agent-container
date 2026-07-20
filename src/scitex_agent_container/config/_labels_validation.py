#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``metadata.labels`` validation — the classification-field SSoT gate.

Split out of ``_validation.py`` to match the sibling focused-validator
convention (``_acl_validation`` / ``_claude_validation`` /
``_placement_validation`` / ``_shape_validation`` /
``_startup_command_validation``) and to keep the orchestrator under the
per-file line cap.

ABOLISHED FIELDS
----------------
``metadata.labels.tags`` — removed by operator decision 2026-07-19.
``groups:`` is the ONLY classification field. The evidence: all 16 fleet
specs that carried ``tags: "active-development"`` ALSO carried ``active``
inside their ``groups:`` list, so ``tags`` carried ZERO information that
``groups`` did not already carry. Pure duplication is the SSoT violation
constitution §1 forbids.

Rejected LOUDLY, with no silent-accept transition window (constitution §2,
no silent fallbacks). A silently-ignored field is exactly how dead fields
survive for months: nothing ever fails, so nobody ever removes them. The
message names the offending FILE because an operator fixing a fleet of
specs needs to know which one failed, and points at the replacement so the
fix is mechanical.
"""

from __future__ import annotations

# Labels that are no longer accepted, mapped to the actionable remedy.
# Keyed by label name; the message is formatted with the spec ``path``.
_ABOLISHED_LABELS: dict[str, str] = {
    "tags": (
        "metadata.labels.tags is no longer accepted in {path}; 'groups' is "
        "the only classification field (operator decision 2026-07-19). "
        "Remove the 'tags:' line and list the value in 'groups:' instead "
        "— e.g. groups: [developer, active]. Filter with "
        "`sac agent status --group NAME` (the --tags flag is gone)."
    ),
}


def validate_labels(metadata: object, path: str) -> list[str]:
    """Return error strings for abolished ``metadata.labels`` entries.

    ``metadata`` is the raw ``metadata`` value straight off the parsed
    YAML — it may legitimately be ``None`` (no metadata block) or a
    non-mapping (already reported by the caller's shape check), in which
    case there is nothing here to validate and the result is empty.

    Deliberately checks KEY PRESENCE, not truthiness: ``tags:`` with an
    empty value is still an authored dead field and must still fail, or
    the abolition leaks through the one spec that blanked it instead of
    deleting it.
    """
    if not isinstance(metadata, dict):
        return []
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        return []
    return [
        message.format(path=path)
        for label, message in _ABOLISHED_LABELS.items()
        if label in labels
    ]


__all__ = ["validate_labels"]
