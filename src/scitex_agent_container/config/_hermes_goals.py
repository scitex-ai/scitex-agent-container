"""Explicit Hermes turn limits and same-engine goal-judge policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness


@dataclass(frozen=True)
class HermesGoalSpec:
    max_turns: int | None = None
    judge_engine: str = ""


def _positive_turns(value: object, path: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return value


def validate_hermes_turn_controls(entry: Mapping, *, path: str) -> None:
    """Validate authored controls without consulting credentials or providers."""
    if "max_turns" in entry:
        _positive_turns(entry["max_turns"], f"{path}.max_turns")
    if "goals" not in entry:
        return
    goals = entry["goals"]
    if not isinstance(goals, Mapping):
        raise ValueError(f"{path}.goals must be a mapping")
    unknown = sorted(set(map(str, goals)) - {"max_turns", "judge_engine"})
    if unknown:
        raise ValueError(f"{path}.goals has unknown fields: {unknown}")
    if "max_turns" in goals:
        _positive_turns(goals["max_turns"], f"{path}.goals.max_turns")
    if "judge_engine" in goals and (
        not isinstance(goals["judge_engine"], str) or not goals["judge_engine"].strip()
    ):
        raise ValueError(f"{path}.goals.judge_engine must be a non-empty engine key")


def _selected_hermes_entry(spec: Mapping) -> Mapping:
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return {}
    harnesses = spec.get("available_harnesses")
    if isinstance(harnesses, Mapping):
        for key, entry in harnesses.items():
            if canonical_harness(str(key)) == "hermes" and isinstance(entry, Mapping):
                validate_hermes_turn_controls(
                    entry, path="spec.available_harnesses.hermes"
                )
                return entry
    return {}


def parse_selected_hermes_max_turns(spec: Mapping) -> int | None:
    return _selected_hermes_entry(spec).get("max_turns")


def parse_selected_hermes_goals(spec: Mapping) -> HermesGoalSpec:
    goals = _selected_hermes_entry(spec).get("goals", {})
    return HermesGoalSpec(
        max_turns=goals.get("max_turns"),
        judge_engine=goals.get("judge_engine", "").strip(),
    )


def validate_hermes_goal_engine(config) -> None:
    """An explicit judge shares the selected engine and its credential pool."""
    judge = config.hermes_goals.judge_engine
    if not judge:
        return
    if judge not in config.engines:
        raise ValueError(
            f"Hermes goals.judge_engine {judge!r} is not a declared engine"
        )
    if judge != config.engine_key:
        raise ValueError("Hermes goals.judge_engine must be the selected agent engine")
    from ._parsers import resolve_model_surface

    model, _ = resolve_model_surface(config.engines[judge].model)
    if model != config.model:
        raise ValueError("Hermes goal judge and agent must use the same model")


__all__ = [
    "HermesGoalSpec",
    "parse_selected_hermes_goals",
    "parse_selected_hermes_max_turns",
    "validate_hermes_goal_engine",
    "validate_hermes_turn_controls",
]
