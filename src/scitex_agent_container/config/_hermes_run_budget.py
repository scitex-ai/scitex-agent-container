"""Parse reproducible Hermes turn limits from the selected harness entry."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

DEFAULT_HERMES_RUN_BUDGET_SECONDS = None
DEFAULT_HERMES_MAX_TURNS = None


def _selected_limit(spec: Mapping, key: str, default: int | None) -> int | None:
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return default
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return default
    for harness_key, value in harnesses.items():
        if canonical_harness(str(harness_key)) == "hermes" and isinstance(
            value, Mapping
        ):
            selected = value.get(key, default)
            return None if selected is None else int(selected)
    return default


def parse_selected_hermes_run_budget(spec: Mapping) -> int | None:
    """Return the selected Hermes run deadline; ``None`` means unlimited."""
    return _selected_limit(
        spec, "run_budget_seconds", DEFAULT_HERMES_RUN_BUDGET_SECONDS
    )


def parse_selected_hermes_max_turns(spec: Mapping) -> int | None:
    """Return the selected Hermes turn cap; ``None`` means unlimited."""
    return _selected_limit(spec, "max_turns", DEFAULT_HERMES_MAX_TURNS)


__all__ = [
    "DEFAULT_HERMES_MAX_TURNS",
    "DEFAULT_HERMES_RUN_BUDGET_SECONDS",
    "parse_selected_hermes_max_turns",
    "parse_selected_hermes_run_budget",
]
