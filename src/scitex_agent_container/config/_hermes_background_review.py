"""Hermes background-review policy from the selected harness."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness


def parse_selected_hermes_background_review(spec: Mapping) -> bool:
    """Return the selected Hermes background-review policy."""
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return False
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return False
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "hermes" and isinstance(value, Mapping):
            return value.get("background_review", False)
    return False


__all__ = ["parse_selected_hermes_background_review"]
