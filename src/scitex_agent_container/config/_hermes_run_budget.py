"""Optional per-run Hermes budget for explicit bounded/eval workers."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

# Agentic SAC sessions run until completion by default.  A positive value is
# still accepted explicitly for one-shot/eval workers with an external ceiling.
DEFAULT_HERMES_RUN_BUDGET_SECONDS: int | None = None


def parse_selected_hermes_run_budget(spec: Mapping) -> int | None:
    """Return the selected Hermes entry's explicit run budget, if any."""
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return DEFAULT_HERMES_RUN_BUDGET_SECONDS
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return DEFAULT_HERMES_RUN_BUDGET_SECONDS
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "hermes" and isinstance(value, Mapping):
            if "run_budget_seconds" not in value:
                return DEFAULT_HERMES_RUN_BUDGET_SECONDS
            budget = value["run_budget_seconds"]
            if type(budget) is not int or budget <= 0:
                raise ValueError("run_budget_seconds must be a positive integer")
            return budget
    return DEFAULT_HERMES_RUN_BUDGET_SECONDS


__all__ = ["DEFAULT_HERMES_RUN_BUDGET_SECONDS", "parse_selected_hermes_run_budget"]
