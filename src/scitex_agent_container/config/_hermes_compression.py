"""Typed Hermes context-compression settings from the selected harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness


@dataclass(frozen=True)
class HermesCompressionSpec:
    """Hermes compression controls emitted into its materialized profile."""

    threshold: float = 0.80
    target_ratio: float = 0.20
    tail_mode: str = "lean"
    in_place: bool = True

    def __post_init__(self) -> None:
        if type(self.threshold) not in {int, float} or not 0 < self.threshold < 1:
            raise ValueError("Hermes compression threshold must be between 0 and 1")
        if (
            type(self.target_ratio) not in {int, float}
            or not 0 < self.target_ratio < self.threshold
        ):
            raise ValueError(
                "Hermes compression target_ratio must be greater than 0 and less "
                "than threshold"
            )
        if self.tail_mode not in {"lean", "legacy"}:
            raise ValueError("Hermes compression tail_mode must be lean or legacy")
        if type(self.in_place) is not bool:
            raise ValueError("Hermes compression in_place must be a boolean")


def parse_selected_hermes_compression(spec: Mapping) -> HermesCompressionSpec:
    """Read compression from the selected Hermes entry, preserving defaults."""
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return HermesCompressionSpec()
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return HermesCompressionSpec()
    entry: Mapping = {}
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "hermes" and isinstance(value, Mapping):
            entry = value
            break
    raw = entry.get("compression")
    if not isinstance(raw, Mapping):
        return HermesCompressionSpec()
    defaults = HermesCompressionSpec()
    return HermesCompressionSpec(
        threshold=float(raw.get("threshold", defaults.threshold)),
        target_ratio=float(raw.get("target_ratio", defaults.target_ratio)),
        tail_mode=str(raw.get("tail_mode", defaults.tail_mode)),
        in_place=raw.get("in_place", defaults.in_place),
    )


__all__ = ["HermesCompressionSpec", "parse_selected_hermes_compression"]
