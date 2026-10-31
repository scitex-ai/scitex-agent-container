"""Opencode tool-approval policy from the selected harness entry.

SAC doctrine: Apptainer binds, identity, network, and injected
credentials are the security boundary, so the harness runs with
approvals off (``never``). ``ask`` is spec-editable for attached-TUI
sessions with a human present; it blocks headless turns.
"""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

OPENCODE_APPROVAL_POLICIES = frozenset({"never", "ask"})
DEFAULT_OPENCODE_APPROVAL_POLICY = "never"


def parse_selected_opencode_approval_policy(spec: Mapping) -> str:
    """Return the selected opencode entry's approval policy."""
    if canonical_harness(resolve_spec_harness(spec)) != "opencode":
        return DEFAULT_OPENCODE_APPROVAL_POLICY
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return DEFAULT_OPENCODE_APPROVAL_POLICY
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "opencode" and isinstance(value, Mapping):
            policy = value.get("approval_policy", DEFAULT_OPENCODE_APPROVAL_POLICY)
            if policy not in OPENCODE_APPROVAL_POLICIES:
                raise ValueError(
                    "approval_policy must be one of "
                    f"{sorted(OPENCODE_APPROVAL_POLICIES)}"
                )
            return policy
    return DEFAULT_OPENCODE_APPROVAL_POLICY


__all__ = [
    "DEFAULT_OPENCODE_APPROVAL_POLICY",
    "OPENCODE_APPROVAL_POLICIES",
    "parse_selected_opencode_approval_policy",
]
