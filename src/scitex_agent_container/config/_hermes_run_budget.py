"""Bound one Hermes run so priority steering reaches a turn boundary."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

# Operator steering is queued by Hermes during a live autonomous run.  A
# twenty-minute default therefore made an immediately delivered Telegram turn
# wait behind one very long run.  Two minutes preserves useful autonomous work
# while making the worst-case boundary explicit and reasonably short.
DEFAULT_HERMES_RUN_BUDGET_SECONDS = 120


def parse_selected_hermes_run_budget(spec: Mapping) -> int:
    """Return the selected Hermes entry's positive run budget in seconds."""
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return DEFAULT_HERMES_RUN_BUDGET_SECONDS
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return DEFAULT_HERMES_RUN_BUDGET_SECONDS
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "hermes" and isinstance(value, Mapping):
            return int(
                value.get("run_budget_seconds", DEFAULT_HERMES_RUN_BUDGET_SECONDS)
            )
    return DEFAULT_HERMES_RUN_BUDGET_SECONDS


__all__ = ["DEFAULT_HERMES_RUN_BUDGET_SECONDS", "parse_selected_hermes_run_budget"]
