"""Optional per-run opencode budget for explicit bounded/eval workers."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

DEFAULT_OPENCODE_RUN_BUDGET_SECONDS: int | None = None


def parse_selected_opencode_run_budget(spec: Mapping) -> int | None:
    """Return the selected opencode entry's explicit run budget, if any."""
    if canonical_harness(resolve_spec_harness(spec)) != "opencode":
        return DEFAULT_OPENCODE_RUN_BUDGET_SECONDS
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return DEFAULT_OPENCODE_RUN_BUDGET_SECONDS
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "opencode" and isinstance(value, Mapping):
            if "run_budget_seconds" not in value:
                return DEFAULT_OPENCODE_RUN_BUDGET_SECONDS
            budget = value["run_budget_seconds"]
            if type(budget) is not int or budget <= 0:
                raise ValueError("run_budget_seconds must be a positive integer")
            return budget
    return DEFAULT_OPENCODE_RUN_BUDGET_SECONDS


__all__ = ["DEFAULT_OPENCODE_RUN_BUDGET_SECONDS", "parse_selected_opencode_run_budget"]
